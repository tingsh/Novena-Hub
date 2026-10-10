"""
Mosquitto Dynamic Security provisioning.

Manages MQTT client credentials for edge gateways by publishing commands to
the Mosquitto dynamic security plugin's control topic.

Requires:
- Mosquitto 2.x with dynamic-security plugin enabled
- An admin client configured in dynamic-security.json
- Settings: MQTT_DYNSEC_ADMIN_USER, MQTT_DYNSEC_ADMIN_PASS
"""

import json
import logging
import secrets
import threading
import time

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("novena_hub")

DYNSEC_TOPIC = "$CONTROL/dynamic-security/v1"
RESPONSE_TOPIC = "$CONTROL/dynamic-security/v1/response"

# Singleton client for dynsec commands (connects on port 1884 with admin creds)
_dynsec_client = None


class DynsecCommandError(RuntimeError):
    """Mosquitto accepted a control message but rejected its command."""


def _command_target(command):
    for key in ("username", "rolename", "groupname"):
        if command.get(key):
            return key, str(command[key])
    return None, None


def _response_matches(command, response):
    if response.get("command") != command.get("command"):
        return False
    return response.get("correlationData") == command.get("correlationData")


def _is_expected_error(error, *, allow_not_found=False, allow_exists=False):
    normalized = str(error or "").lower()
    if allow_not_found and any(token in normalized for token in ("not found", "does not exist")):
        return True
    return allow_exists and "already" in normalized


def _get_dynsec_client():
    """Get or create the singleton MQTT client for dynamic security commands."""
    global _dynsec_client
    if _dynsec_client is not None and _dynsec_client.is_connected():
        return _dynsec_client

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="novena-dynsec-provisioner",
        protocol=mqtt.MQTTv311,
    )

    admin_user = getattr(settings, "MQTT_DYNSEC_ADMIN_USER", "dynsec-admin")
    admin_pass = getattr(settings, "MQTT_DYNSEC_ADMIN_PASS", "dynsec-password")
    client.username_pw_set(admin_user, admin_pass)

    dynsec_port = getattr(settings, "MQTT_DYNSEC_PORT", 1884)

    try:
        client.connect(settings.MQTT_BROKER_HOST, dynsec_port, 60)
        client.loop_start()
        # Brief wait for connection
        for _ in range(10):
            if client.is_connected():
                break
            time.sleep(0.1)
        _dynsec_client = client
    except Exception as e:
        logger.error("Failed to connect dynsec MQTT client: %s", e)
        raise

    return _dynsec_client


def _publish_dynsec_command(command, *, allow_not_found=False, allow_exists=False):
    """Publish one command and require a matching Dynamic Security response."""
    target_key, target_value = _command_target(command)
    correlation_data = (
        f"{command.get('command')}:{target_key or 'none'}:{target_value or 'none'}:{secrets.token_urlsafe(12)}"
    )
    command = {**command, "correlationData": correlation_data}
    timeout = float(getattr(settings, "MQTT_DYNSEC_RESPONSE_TIMEOUT_SECONDS", 5))
    lock_key = "novena:mqtt:dynsec-command-lock"
    lock_token = secrets.token_urlsafe(18)
    lock_deadline = time.monotonic() + timeout
    while not cache.add(lock_key, lock_token, timeout=max(10, int(timeout * 2))):
        if time.monotonic() >= lock_deadline:
            raise RuntimeError("Timed out waiting for the MQTT security command lock.")
        time.sleep(0.05)

    response_event = threading.Event()
    connected_event = threading.Event()
    matched_response = {}
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"novena-dynsec-{secrets.token_hex(8)}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(
        getattr(settings, "MQTT_DYNSEC_ADMIN_USER", "dynsec-admin"),
        getattr(settings, "MQTT_DYNSEC_ADMIN_PASS", "dynsec-password"),
    )

    def on_connect(inner_client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            inner_client.subscribe(RESPONSE_TOPIC, qos=1)
            connected_event.set()

    def on_message(inner_client, userdata, message):
        try:
            payload = json.loads(message.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        for response in payload.get("responses", []):
            if _response_matches(command, response):
                matched_response.update(response)
                response_event.set()
                return

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(
            settings.MQTT_BROKER_HOST,
            int(getattr(settings, "MQTT_DYNSEC_PORT", 1884)),
            60,
        )
        client.loop_start()
        if not connected_event.wait(timeout):
            raise RuntimeError("Could not subscribe for the MQTT security response.")
        result = client.publish(DYNSEC_TOPIC, json.dumps({"commands": [command]}), qos=1)
        result.wait_for_publish(timeout=timeout)
        if result.rc != mqtt.MQTT_ERR_SUCCESS or not result.is_published():
            raise RuntimeError(f"Dynsec command {command.get('command')} was not acknowledged by the broker.")
        if not response_event.wait(timeout):
            raise RuntimeError(f"Dynsec command {command.get('command')} produced no verified response.")
        error = matched_response.get("error")
        if error and not _is_expected_error(
            error,
            allow_not_found=allow_not_found,
            allow_exists=allow_exists,
        ):
            raise DynsecCommandError(f"Dynsec {command.get('command')} failed: {error}")
        logger.debug("Dynsec command verified: %s", command.get("command"))
        return matched_response
    finally:
        client.loop_stop()
        client.disconnect()
        if cache.get(lock_key) == lock_token:
            cache.delete(lock_key)


def provision_gateway_mqtt(gateway, password):
    """
    Create an MQTT client in Mosquitto for a newly registered gateway.

    Creates the client with the given password and assigns:
    - 'gateway' role (shared baseline role; no cross-gateway inbound publishes)
    - Per-gateway topic ACLs for its serial-number-scoped topics

    Args:
        gateway: Gateway model instance (must have serial_number set)
        password: str — plaintext password (Mosquitto hashes it internally)
    """
    sn = gateway.serial_number
    bootstrap_username = f"bootstrap:{sn}"

    # Step 1: Create the client with password and assign the shared gateway role
    create_cmd = {
        "command": "createClient",
        "username": sn,
        "password": password,
        "textname": f"Edge Gateway {sn}",
        "textdescription": f"Auto-provisioned for gateway {gateway.name}",
        "roles": [{"rolename": "gateway", "priority": -1}],
    }
    _publish_dynsec_command(create_cmd, allow_exists=True)

    # Step 2: Create a per-gateway role with scoped topic ACLs
    role_name = f"gw-{sn}"
    create_role_cmd = {
        "command": "createRole",
        "rolename": role_name,
        "textdescription": f"Per-gateway ACLs for {sn}",
        "acls": [
            # Edge → cloud publishes for this gateway only.
            {"acltype": "publishClientSend", "topic": f"v1/gateway/{sn}/telemetry", "allow": True},
            {"acltype": "publishClientSend", "topic": f"v1/gateway/{sn}/logs", "allow": True},
            {"acltype": "publishClientSend", "topic": f"v1/gateway/{sn}/attributes", "allow": True},
            {"acltype": "publishClientSend", "topic": f"v1/gateway/{sn}/rpc/response", "allow": True},
            # Subscribe to per-gateway inbound topics (cloud → edge)
            {"acltype": "subscribePattern", "topic": f"v1/gateway/{sn}/#", "allow": True},
            # Receive messages on per-gateway topics
            {"acltype": "publishClientReceive", "topic": f"v1/gateway/{sn}/#", "allow": True},
        ],
    }
    _publish_dynsec_command(create_role_cmd, allow_exists=True)

    # Step 3: Set the managed roles idempotently. Mosquitto 2.0 can return
    # "Internal error" when addClientRole repeats an existing assignment.
    # Replacing the managed role list also avoids retaining unintended grants.
    set_roles_cmd = {
        "command": "modifyClient",
        "username": sn,
        "roles": [
            {"rolename": "gateway", "priority": -1},
            {"rolename": role_name, "priority": -1},
        ],
    }
    _publish_dynsec_command(set_roles_cmd)

    # Step 4: Create a bootstrap client scoped to only first-time activation.
    # The physical gateway uses this identity after operational auth failures.
    bootstrap_role_name = f"bootstrap-gw-{sn}"
    bootstrap_role_cmd = {
        "command": "createRole",
        "rolename": bootstrap_role_name,
        "textdescription": f"Bootstrap activation ACLs for {sn}",
        "acls": [
            {"acltype": "publishClientSend", "topic": f"v1/gateway/{sn}/bootstrap/hello", "allow": True},
            {"acltype": "subscribePattern", "topic": f"v1/gateway/{sn}/bootstrap/activate", "allow": True},
            {"acltype": "publishClientReceive", "topic": f"v1/gateway/{sn}/bootstrap/activate", "allow": True},
        ],
    }
    _publish_dynsec_command(bootstrap_role_cmd, allow_exists=True)

    from .services import compute_claim_code

    bootstrap_client_cmd = {
        "command": "createClient",
        "username": bootstrap_username,
        "password": compute_claim_code(sn),
        "textname": f"Bootstrap Gateway {sn}",
        "textdescription": f"Claim-time activation client for gateway {gateway.name}",
        "roles": [{"rolename": bootstrap_role_name, "priority": -1}],
    }
    _publish_dynsec_command(bootstrap_client_cmd, allow_exists=True)

    # Existing clients need the new activation generation even when createClient
    # was an idempotent no-op.
    _publish_dynsec_command(
        {"command": "modifyClient", "username": sn, "password": password},
    )
    _publish_dynsec_command(
        {
            "command": "modifyClient",
            "username": bootstrap_username,
            "password": compute_claim_code(sn),
        },
    )

    logger.info("Provisioned MQTT credentials for gateway %s on Mosquitto", sn)


def rotate_gateway_password(gateway, new_password):
    """
    Rotate an MQTT client's password in Mosquitto via dynamic security.

    Args:
        gateway: Gateway model instance
        new_password: str — new plaintext password
    """
    sn = gateway.serial_number

    modify_cmd = {
        "command": "modifyClient",
        "username": sn,
        "password": new_password,
    }
    _publish_dynsec_command(modify_cmd)

    logger.info("Rotated MQTT password for gateway %s on Mosquitto", sn)


def deprovision_gateway_mqtt(gateway):
    """
    Remove an MQTT client and its per-gateway role from Mosquitto.

    Args:
        gateway: Gateway model instance
    """
    sn = gateway.serial_number

    # Disable both identities first. A confirmed disable/delete immediately
    # disconnects clients and makes partial retries fail closed.
    _publish_dynsec_command(
        {"command": "disableClient", "username": sn},
        allow_not_found=True,
    )
    _publish_dynsec_command(
        {"command": "disableClient", "username": f"bootstrap:{sn}"},
        allow_not_found=True,
    )

    # Delete the client
    delete_client_cmd = {
        "command": "deleteClient",
        "username": sn,
    }
    _publish_dynsec_command(delete_client_cmd, allow_not_found=True)

    # Delete the per-gateway role
    role_name = f"gw-{sn}"
    delete_role_cmd = {
        "command": "deleteRole",
        "rolename": role_name,
    }
    _publish_dynsec_command(delete_role_cmd, allow_not_found=True)

    # Delete the claim-time bootstrap identity and its scoped role.
    delete_bootstrap_client_cmd = {
        "command": "deleteClient",
        "username": f"bootstrap:{sn}",
    }
    _publish_dynsec_command(delete_bootstrap_client_cmd, allow_not_found=True)

    delete_bootstrap_role_cmd = {
        "command": "deleteRole",
        "rolename": f"bootstrap-gw-{sn}",
    }
    _publish_dynsec_command(delete_bootstrap_role_cmd, allow_not_found=True)

    logger.info("Deprovisioned MQTT credentials for gateway %s from Mosquitto", sn)
