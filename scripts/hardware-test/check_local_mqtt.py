#!/usr/bin/env python3
"""Read-only authentication checks; never equate TCP availability with security."""

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "novena_hub.settings")
import django
import paho.mqtt.client as mqtt

django.setup()
from django.conf import settings  # noqa: E402

from apps.devices.mqtt_provisioning import _publish_dynsec_command  # noqa: E402


def connack(host, port, username=None, password=None):
    event = threading.Event()
    codes = []
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    if username:
        client.username_pw_set(username, password)

    def connected(client, userdata, flags, code, properties):
        codes.append(code.value)
        event.set()

    client.on_connect = connected
    try:
        client.connect(host, int(port))
        client.loop_start()
        if not event.wait(5):
            raise RuntimeError("No CONNACK received; broker security was not verified.")
        return codes[0]
    finally:
        client.disconnect()
        client.loop_stop()


def verify_lifecycle():
    """Use a unique broker-only identity; never claim/release customer database rows."""
    import secrets
    from types import SimpleNamespace

    from apps.devices.mqtt_provisioning import deprovision_gateway_mqtt, provision_gateway_mqtt
    from apps.devices.services import compute_claim_code

    serial = "NOV-SECURITY-PROBE-" + secrets.token_hex(8)
    gateway = SimpleNamespace(serial_number=serial, name="Disposable broker verification")
    password = secrets.token_urlsafe(32)
    host, port = settings.PUBLIC_MQTT_BROKER_HOST, settings.PUBLIC_MQTT_BROKER_PORT
    try:
        provision_gateway_mqtt(gateway, password)
        for username, secret in ((serial, password), ("bootstrap:" + serial, compute_claim_code(serial))):
            if connack(host, port, username, secret) != 0:
                raise RuntimeError("Admin provisioning was not visible on the Gateway listener.")
        deprovision_gateway_mqtt(gateway)
        for username, secret in ((serial, password), ("bootstrap:" + serial, compute_claim_code(serial))):
            if connack(host, port, username, secret) not in (134, 135):
                raise RuntimeError("Revoked credential was not explicitly rejected by the Gateway listener.")
    finally:
        deprovision_gateway_mqtt(gateway)
    print("Installed broker provision/revoke verified across listeners; old identities rejected.")


def main():
    if not settings.MQTT_PROVISIONING_REQUIRED:
        print("MQTT provisioning intentionally disabled; credential revocation is NOT tested.")
        return
    if settings.MQTT_BROKER_HOST not in ("localhost", "127.0.0.1") or settings.MQTT_BROKER_PORT != 1885:
        raise RuntimeError("Local Dynamic Security requires Hub loopback MQTT on port 1885.")
    _publish_dynsec_command({"command": "getRole", "rolename": "gateway"})
    if connack(settings.PUBLIC_MQTT_BROKER_HOST, settings.PUBLIC_MQTT_BROKER_PORT) not in (134, 135):
        raise RuntimeError("Gateway listener failed to reject anonymous authentication.")
    if connack("127.0.0.1", 1885) != 0:
        raise RuntimeError("Internal Hub listener rejected its local connection.")
    print("Dynamic Security responded; LAN listener rejects anonymous clients; internal MQTT available.")


if __name__ == "__main__":
    main()
