#!/usr/bin/env python3
"""Initialize the production broker without exposing its admin password in argv."""

import json
import os
import subprocess
from pathlib import Path


ROOT = Path("/opt/novena/Novena-Hub")
ENV_FILE = ROOT / "deploy/env/production.env"
DATA_DIR = ROOT / "deploy/mosquitto/data"
STATE_FILE = DATA_DIR / "dynamic-security.json"
INITIAL_FILE = DATA_DIR / ".dynamic-security-initial.json"
STAGED_FILE = DATA_DIR / ".dynamic-security-staged.json"
BROKER_ID = 1883


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo so broker state can be made private.")
    if STATE_FILE.exists() or INITIAL_FILE.exists() or STAGED_FILE.exists():
        raise SystemExit("Broker state already exists; refusing to replace it.")

    values = dict(
        line.split("=", 1)
        for line in ENV_FILE.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    username = values.get("MQTT_DYNSEC_ADMIN_USER", "")
    password = values.get("MQTT_DYNSEC_ADMIN_PASS", "")
    if not username or not password or username.startswith("replace-") or password.startswith("replace-"):
        raise SystemExit("Set the MQTT Dynamic Security admin credentials first.")
    if any(character.isspace() for character in username + password):
        raise SystemExit("MQTT admin credentials must not contain whitespace.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(DATA_DIR, BROKER_ID, ROOT.stat().st_gid)
    DATA_DIR.chmod(0o750)
    try:
        subprocess.run(
            [
                "docker", "run", "--rm", "-i", "--user", "1883:1883",
                "--mount", f"type=bind,src={DATA_DIR},dst=/mosquitto/data",
                "eclipse-mosquitto:2.1.4-alpine", "mosquitto_ctrl", "dynsec", "init",
                "/mosquitto/data/.dynamic-security-initial.json", username,
            ],
            input=f"{password}\n{password}\n",
            text=True,
            capture_output=True,
            check=True,
        )
        state = json.loads(INITIAL_FILE.read_text())
        if username not in {client.get("username") for client in state.get("clients", [])}:
            raise RuntimeError("Mosquitto did not create the expected admin client.")
        state["defaultACLAccess"] = {
            "publishClientSend": False,
            "publishClientReceive": False,
            "subscribe": False,
            "unsubscribe": True,
        }
        if not any(role.get("rolename") == "gateway" for role in state.get("roles", [])):
            state["roles"].append({"rolename": "gateway", "acls": []})
        with STAGED_FILE.open("x") as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
        os.chown(STAGED_FILE, BROKER_ID, BROKER_ID)
        STAGED_FILE.chmod(0o600)
        STAGED_FILE.replace(STATE_FILE)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Mosquitto initialization failed (exit {exc.returncode}).") from exc
    finally:
        INITIAL_FILE.unlink(missing_ok=True)
        STAGED_FILE.unlink(missing_ok=True)

    print("Broker admin identity and empty gateway role initialized; no credentials printed.")


if __name__ == "__main__":
    main()
