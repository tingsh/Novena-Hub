import json
import logging
import uuid

import paho.mqtt.client as mqtt
from django.conf import settings

logger = logging.getLogger("novena_hub")

# Singleton MQTT client for publishing (Cloud → Edge)
_client = None
_connected = False


class MqttPublishOutcomeUnknown(RuntimeError):
    """The broker acknowledgement was not observed; callers must not repeat a write."""

    def __init__(self, message, *, rpc_record):
        super().__init__(message)
        self.rpc_record = rpc_record


def get_mqtt_client():
    """Get or create the singleton MQTT publish client."""
    global _client, _connected
    if _client is not None:
        return _client

    _client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=settings.MQTT_PUBLISHER_CLIENT_ID,
        protocol=mqtt.MQTTv311,
    )

    def on_connect(client, userdata, flags, rc, properties=None):
        global _connected
        if rc == 0:
            _connected = True
            logger.info("MQTT publisher connected to broker")
        else:
            logger.error("MQTT publisher connection failed: %s", rc)

    def on_disconnect(client, userdata, flags, rc, properties=None):
        global _connected
        _connected = False
        logger.warning("MQTT publisher disconnected")

    _client.on_connect = on_connect
    _client.on_disconnect = on_disconnect

    try:
        _client.connect(settings.MQTT_BROKER_HOST, settings.MQTT_BROKER_PORT, 60)
        _client.loop_start()
    except Exception as e:
        logger.error("Failed to connect MQTT publisher: %s", e)

    return _client


def _publish_qos1(topic: str, payload: dict):
    client = get_mqtt_client()
    result = client.publish(topic, json.dumps(payload), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish rejected locally (rc={result.rc}).")
    try:
        result.wait_for_publish(timeout=5)
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError("MQTT broker acknowledgement was not observed.") from exc
    if not result.is_published():
        raise RuntimeError("MQTT broker acknowledgement timed out.")
    return result


def publish_config_envelope(gateway, envelope: dict):
    """Publish an already persisted, signed config envelope."""
    topic = f"v1/gateway/{gateway.serial_number}/config"
    result = _publish_qos1(topic, envelope)
    logger.info(
        "Published signed config to %s (request_id=%s, revision=%s, rc=%s)",
        gateway.serial_number,
        envelope.get("request_id"),
        envelope.get("revision"),
        result.rc,
    )
    return result


def publish_rpc_command(
    gateway,
    method,
    params=None,
    *,
    remote_command=None,
    request_id=None,
    governed_envelope=None,
):
    """
    Send an RPC command to a gateway.

    Args:
        gateway: Gateway model instance
        method: str — command method name (ping, get_status, write_device, etc.)
        params: dict — command parameters

    Returns:
        RpcCommand instance
    """
    from apps.devices.models import RpcCommand

    request_id = request_id or uuid.uuid4()
    topic = f"v1/gateway/{gateway.serial_number}/rpc/request"
    if governed_envelope:
        if (
            str(governed_envelope.get("request_id")) != str(request_id)
            or governed_envelope.get("method") != method
            or governed_envelope.get("params") != (params or {})
        ):
            raise ValueError("Governed envelope does not match the persisted RPC transport record.")
        payload = governed_envelope
    else:
        payload = {
            "request_id": str(request_id),
            "method": method,
            "params": params or {},
        }

    # Store in DB
    rpc_record = RpcCommand.objects.create(
        team=gateway.team,
        gateway=gateway,
        request_id=request_id,
        method=method,
        params=params or {},
        remote_command=remote_command,
    )

    # Publish
    client = get_mqtt_client()
    result = client.publish(topic, json.dumps(payload), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        rpc_record.status = "error"
        rpc_record.error_message = f"MQTT publish rejected locally (rc={result.rc})."
        rpc_record.save(update_fields=["status", "error_message", "updated_at"])
        raise RuntimeError(rpc_record.error_message)
    try:
        result.wait_for_publish(timeout=5)
    except (RuntimeError, ValueError) as exc:
        raise MqttPublishOutcomeUnknown(
            "MQTT publish acknowledgement was not observed; delivery outcome is unknown.",
            rpc_record=rpc_record,
        ) from exc
    if not result.is_published():
        raise MqttPublishOutcomeUnknown(
            "MQTT publish acknowledgement timed out; delivery outcome is unknown.",
            rpc_record=rpc_record,
        )
    logger.info(
        "Published RPC command '%s' to %s (request_id=%s, rc=%s)",
        method,
        gateway.serial_number,
        request_id,
        result.rc,
    )

    return rpc_record


def publish_credential_rotation(gateway, new_password):
    """
    Publish a credential rotation command to the Edge via the provision topic.
    The Edge will save the new password and reconnect.

    Args:
        gateway: Gateway model instance
        new_password: str — new plaintext password
    """
    topic = f"v1/gateway/{gateway.serial_number}/provision"
    payload = {
        "action": "rotate_password",
        "new_password": new_password,
    }

    client = get_mqtt_client()
    result = client.publish(topic, json.dumps(payload), qos=1)
    logger.info(
        "Published credential rotation to %s (rc=%s)",
        gateway.serial_number,
        result.rc,
    )


def publish_gateway_activation(activation, operational_password):
    """
    Send fresh operational MQTT credentials while the gateway is in bootstrap mode.
    """
    gateway = activation.gateway
    from apps.telemetry.claim_identity import gateway_claim_id
    request_id = str(activation.request_id)
    topic = f"v1/gateway/{gateway.serial_number}/bootstrap/activate"
    payload = {
        "request_id": request_id,
        "generation": activation.generation,
        "action": "activate",
        "claim_id": gateway_claim_id(gateway),
        "activation_expires_at": activation.expires_at.isoformat(),
        "mqtt": {
            "username": gateway.mqtt_username or gateway.serial_number,
            "password": operational_password,
            "client_id": f"novena-gateway-{gateway.serial_number}",
        },
    }

    result = _publish_qos1(topic, payload)
    logger.info(
        "Published gateway activation to %s (request_id=%s, rc=%s)",
        gateway.serial_number,
        request_id,
        result.rc,
    )
    return request_id


def publish_gateway_release(release):
    """Ask the currently claimed edge to erase site state before broker revocation."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.devices.remote_control_crypto import sign_payload
    from apps.telemetry.claim_identity import gateway_claim_id

    gateway = release.gateway
    body = {
        "schema_version": 1,
        "action": "release",
        "request_id": str(release.request_id),
        "claim_id": gateway_claim_id(gateway),
        "target": {"gateway_serial": gateway.serial_number},
        "expires_at": (timezone.now() + timedelta(minutes=5)).isoformat(),
    }
    key_id, signature = sign_payload(body)
    topic = f"v1/gateway/{gateway.serial_number}/provision"
    return _publish_qos1(topic, {**body, "signing_key_id": key_id, "signature": signature})


def clear_gateway_retained_policy(gateway):
    """Remove old owner's retained control policy before serial reuse."""
    topic = f"v1/gateway/{gateway.serial_number}/control/policy"
    result = get_mqtt_client().publish(topic, b"", qos=1, retain=True)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError("Could not clear retained Gateway control policy.")
    result.wait_for_publish(timeout=5)
    if not result.is_published():
        raise RuntimeError("Broker did not confirm retained policy removal.")


def publish_attribute_push(gateway, attributes):
    """
    Push attributes to a gateway.

    Args:
        gateway: Gateway model instance
        attributes: dict — key-value pairs to push
    """
    request_id = str(uuid.uuid4())
    topic = f"v1/gateway/{gateway.serial_number}/attributes/request"
    payload = {
        "request_id": request_id,
        "attributes": attributes,
    }

    client = get_mqtt_client()
    result = client.publish(topic, json.dumps(payload), qos=1)
    logger.info(
        "Published attribute push to %s (request_id=%s, rc=%s)",
        gateway.serial_number,
        request_id,
        result.rc,
    )


def publish_control_policy_bundle(bundle):
    """Publish a signed retained policy so the edge can independently default-deny."""
    topic = f"v1/gateway/{bundle.gateway.serial_number}/control/policy"
    wire_payload = {
        "payload": bundle.payload,
        "checksum": bundle.checksum,
        "signing_key_id": bundle.signing_key_id,
        "signature": bundle.signature,
    }
    client = get_mqtt_client()
    result = client.publish(topic, json.dumps(wire_payload), qos=1, retain=True)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT policy publish rejected locally (rc={result.rc}).")
    result.wait_for_publish(timeout=5)
    if not result.is_published():
        raise RuntimeError("Policy publish acknowledgement timed out.")

    from django.utils import timezone

    bundle.published_at = timezone.now()
    bundle.save(update_fields=["published_at", "updated_at"])
    return bundle
