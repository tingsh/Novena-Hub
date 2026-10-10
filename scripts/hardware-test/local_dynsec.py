#!/usr/bin/env python3
"""Stage local Mosquitto configuration; activate Hub settings only after installation.

Never changes system files. Secrets stay in mode-0600 files, not argv/output.
"""

import argparse
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def private_write(path, content):
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        stream.write(content)


def validate_state(state):
    """Refuse legacy schemas and broad gateway permissions; never auto-convert hashes."""
    roles = {r.get("rolename"): r for r in state.get("roles", [])}
    if None in roles or not roles:
        raise ValueError("Legacy roleName schema: preserve original; initialize a separate local state.")
    defaults = state.get("defaultACLAccess", {})
    if any(defaults.get(k, True) for k in ("publishClientSend", "publishClientReceive", "subscribe")):
        raise ValueError("Default ACLs must deny publish, receive and subscribe.")
    if state.get("groups") or state.get("anonymousGroup"):
        raise ValueError("Existing groups require manual permission review.")
    for client in state.get("clients", []):
        if not isinstance(client.get("password"), str) or not client.get("salt") or not client.get("iterations"):
            raise ValueError("Client has an incompatible or missing password hash; preserve original.")
        for role in client.get("roles", []):
            if role.get("rolename") not in roles:
                raise ValueError("Client has an unknown/legacy role reference.")
    if roles.get("gateway", {}).get("acls"):
        raise ValueError("Shared gateway role must have no ACLs; review existing permissions.")
    for name, role in roles.items():
        if name.startswith(("gw-", "bootstrap-gw-")):
            serial = name.removeprefix("bootstrap-gw-") if name.startswith("bootstrap-gw-") else name[3:]
            for acl in role.get("acls", []):
                if acl.get("allow") and not acl.get("topic", "").startswith(f"v1/gateway/{serial}/"):
                    raise ValueError("Gateway role permits topics outside its serial namespace.")


def stage(args):
    address = ipaddress.ip_address(args.lan_ip)
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified:
        raise ValueError("Use a specific private IPv4 LAN address.")
    if not args.admin_user or any(character in args.admin_user for character in "\r\n= "):
        raise ValueError("Administrator name must not contain whitespace or equals signs.")
    target = args.directory.resolve()
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    if args.source:
        state = json.loads(args.source.read_text())
        validate_state(state)
        # Reuse only with the matching administrator secret supplied in a private file.
        if not args.admin_password_file or args.admin_password_file.stat().st_mode & 0o077:
            raise ValueError("Reuse requires a mode-0600 administrator password file.")
        password = args.admin_password_file.read_text().strip()
        if not password or any(character in password for character in "\r\n"):
            raise ValueError("Administrator password file must contain one nonempty line.")
        if args.admin_user not in {c["username"] for c in state["clients"]}:
            raise ValueError("Administrator is absent from the preserved state.")
    else:
        password = secrets.token_urlsafe(36)
        subprocess.run(
            ["mosquitto_ctrl", "dynsec", "init", str(target / "initial.json"), args.admin_user],
            input=f"{password}\n{password}\n",
            text=True,
            capture_output=True,
            check=True,
        )
        state = json.loads((target / "initial.json").read_text())
        (target / "initial.json").unlink()
        state["defaultACLAccess"] = dict(
            publishClientSend=False, publishClientReceive=False, subscribe=False, unsubscribe=True
        )
    if not any(r["rolename"] == "gateway" for r in state["roles"]):
        state["roles"].append({"rolename": "gateway", "acls": []})
    validate_state(state)
    private_write(target / "dynamic-security.json", json.dumps(state, indent=2) + "\n")
    template = ROOT / "deploy/mosquitto/local/novena-local-replay.conf"
    private_write(target / "novena-local-replay.conf", template.read_text().replace("@LAN_IP@", str(address)))
    values = dict(
        MQTT_BROKER_HOST="127.0.0.1",
        MQTT_BROKER_PORT="1885",
        MQTT_DYNSEC_PORT="1884",
        MQTT_DYNSEC_ADMIN_USER=args.admin_user,
        MQTT_DYNSEC_ADMIN_PASS=password,
        MQTT_PROVISIONING_REQUIRED="True",
        PUBLIC_MQTT_BROKER_SCHEME="mqtt",
        PUBLIC_MQTT_BROKER_HOST=str(address),
        PUBLIC_MQTT_BROKER_PORT="1883",
    )
    private_write(target / "hub-settings.json", json.dumps(values))
    print(f"Staged private configuration in {target}. Install and verify before activate.")


def activate(args):
    values = json.loads((args.directory / "hub-settings.json").read_text())
    # Verify a correlated response on the installed administrative listener first.
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "novena_hub.settings")
    import django

    django.setup()
    from django.conf import settings
    from django.test import override_settings

    from apps.devices.mqtt_provisioning import _publish_dynsec_command

    if not settings.DEBUG or getattr(settings, "NOVENA_DEPLOYMENT_MODE", "local") != "local":
        raise ValueError("This installer activation is restricted to local DEBUG mode.")
    typed_values = {key: int(value) if key.endswith("_PORT") else value for key, value in values.items()}
    typed_values["MQTT_PROVISIONING_REQUIRED"] = True
    with override_settings(**typed_values):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from check_local_mqtt import main as check_local_mqtt
        from check_local_mqtt import verify_lifecycle

        check_local_mqtt()
        verify_lifecycle()
        _publish_dynsec_command({"command": "getRole", "rolename": "gateway"})
    env = ROOT / ".env"
    backup = ROOT / "backups" / ("mqtt-dynsec-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(env, backup / ".env")
    (backup / ".env").chmod(0o600)
    lines = env.read_text().splitlines()
    output = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    output.extend(f"{key}={value}" for key, value in values.items())
    temp = env.with_name(".env.dynsec-new")
    private_write(temp, "\n".join(output) + "\n")
    temp.replace(env)
    print(f"Hub environment activated; backup: {backup}. Restart Hub, worker and MQTT consumer.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage", "activate"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--lan-ip")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--admin-user", default="novena-local-admin")
    parser.add_argument("--admin-password-file", type=Path)
    args = parser.parse_args()
    try:
        (stage if args.action == "stage" else activate)(args)
    except (ValueError, OSError, subprocess.SubprocessError):
        # Avoid accidentally including secrets from a subprocess or JSON value.
        parser.exit(1, "Setup failed. Check paths, state schema/ACLs, file permissions and broker availability.\n")


if __name__ == "__main__":
    main()
