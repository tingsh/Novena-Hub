"""
Edge Connector Config Generator

Transforms Cloud DeviceTemplate register maps into Edge-compatible connector
configurations that can be pushed via MQTT config update.

The generated config follows the Novena Gateway connector schema:
- Groups devices by protocol (modbus_tcp, modbus_rtu, etc.)
- Emits Edge connector objects with top-level type/name and nested config
- Includes device_id (UUID) for unambiguous telemetry matching
"""

import logging
import math

from .datapoint_maps import effective_register_map, register_map_to_datapoints

logger = logging.getLogger("novena_hub")

MODBUS_TYPE_MAP = {
    "float32": "32float",
    "float64": "64float",
    "int16": "16int",
    "uint16": "16uint",
    "int32": "32int",
    "uint32": "32uint",
    "int64": "64int",
    "uint64": "64uint",
    "bool": "bits",
}


def generate_connector_config(gateway):
    """
    Generate the complete Edge connector config from all devices
    registered on this gateway + their templates.

    Returns a list of connector config dicts ready to push to the Edge.
    """
    devices = gateway.devices.select_related("template").all()

    protocol_groups = {}
    for device in devices:
        validation_state = (device.metadata or {}).get("guided_setup_validation")
        if validation_state and validation_state != "validated":
            logger.info("Device %s is not validated yet; excluding it from config", device.name)
            continue
        protocol = device.protocol or "modbus_tcp"
        protocol_groups.setdefault(protocol, []).append(device)

    connectors = []
    for protocol, devices_in_group in protocol_groups.items():
        if protocol in ("modbus_tcp", "modbus_rtu"):
            connector = _build_modbus_connector(protocol, devices_in_group)
            if connector:
                connectors.append(connector)
        else:
            logger.warning("Unsupported protocol '%s' for config generation, skipping", protocol)

    return connectors


def _build_modbus_connector(protocol, devices):
    """Build an Edge-compatible Modbus connector config."""
    is_tcp = protocol == "modbus_tcp"
    connector_name = "Modbus TCP Connector" if is_tcp else "Modbus RTU Connector"

    slaves = []
    for device in devices:
        slave = _build_slave_config(device, is_tcp)
        if slave:
            slaves.append(slave)

    if not slaves:
        return None

    return {
        "name": connector_name,
        "type": "modbus",
        "config": {
            "master": {
                "slaves": slaves,
            },
        },
    }


def _build_slave_config(device, is_tcp):
    """Build a single Modbus slave config from a Device + DeviceTemplate."""
    template = device.template
    register_map = effective_register_map(device)
    if not template or not register_map:
        logger.info("Device %s has no confirmed datapoint map, skipping config generation", device.name)
        return None

    discovery = device.discovery_meta or {}
    connection = device.connection_config or {}

    from apps.subscriptions.enforcement import get_effective_polling_interval_seconds

    effective_polling_seconds = get_effective_polling_interval_seconds(device)
    slave = {
        "deviceName": device.name,
        "deviceId": str(device.pk),
        "unitId": discovery.get("slave_id", connection.get("slave_id", 1)),
        "pollPeriod": math.ceil(effective_polling_seconds * 1000),
        "method": connection.get("method", "socket"),
        "timeout": connection.get("timeout", 35),
        "byteOrder": connection.get("byteOrder", "BIG"),
        "wordOrder": connection.get("wordOrder", "BIG"),
        "retries": connection.get("retries", True),
        "retryOnEmpty": connection.get("retryOnEmpty", True),
        "retryOnInvalid": connection.get("retryOnInvalid", True),
        "sendDataOnlyOnChange": connection.get("sendDataOnlyOnChange", False),
        "connectAttemptTimeMs": connection.get("connectAttemptTimeMs", 5000),
        "connectAttemptCount": connection.get("connectAttemptCount", 5),
        # Retry within the device's offline window after the attempt budget is
        # exhausted. Five minutes leaves a recovered field device stale while
        # the Gateway heartbeat remains healthy.
        "waitAfterFailedAttemptsMs": connection.get("waitAfterFailedAttemptsMs", 30000),
        "timeseries": [],
        "attributes": [],
    }

    if is_tcp:
        interface = discovery.get("interface", "")
        if ":" in interface:
            host, port = interface.rsplit(":", 1)
            slave["host"] = host
            slave["port"] = int(port) if port.isdigit() else 502
        else:
            slave["host"] = connection.get("host", interface or "127.0.0.1")
            slave["port"] = connection.get("port", 502)
        slave["type"] = "tcp"
    else:
        slave["type"] = "serial"
        slave["port"] = discovery.get("interface", connection.get("serial_port", "/dev/ttyUSB0"))
        slave["baudrate"] = discovery.get("baud_rate", connection.get("baudrate", 9600))
        slave["stopbits"] = connection.get("stopbits", 1)
        slave["bytesize"] = connection.get("bytesize", 8)
        slave["parity"] = connection.get("parity", "N")

    for key, reg in register_map.items():
        if not isinstance(reg, dict):
            continue
        entry = {
            "tag": key,
            "type": _normalize_modbus_type(reg.get("type", "16int")),
            "functionCode": reg.get("readFunctionCode", reg.get("functionCode", 3)),
            "objectsCount": reg.get("objectsCount", _default_objects_count(reg.get("type"))),
            "address": reg.get("address", 0),
        }
        if "scale" in reg:
            entry["multiplier"] = reg["scale"]
        if "multiplier" in reg:
            entry["multiplier"] = reg["multiplier"]
        if "offset" in reg:
            entry["offset"] = reg["offset"]
        if "invalidValues" in reg:
            entry["invalidValues"] = reg["invalidValues"]

        if reg.get("attribute"):
            slave["attributes"].append(entry)
        elif reg.get("poll", not reg.get("writable")):
            slave["timeseries"].append(entry)

    return slave


def _normalize_modbus_type(type_name):
    if not type_name:
        return "16int"
    normalized = str(type_name).replace("_", "").lower()
    return MODBUS_TYPE_MAP.get(normalized, type_name)


def _default_objects_count(type_name):
    normalized = _normalize_modbus_type(type_name)
    if normalized.startswith("64"):
        return 4
    if normalized.startswith("32"):
        return 2
    return 1


def normalized_datapoints(template, *, team=None):
    """Expose the legacy register map through a versioned protocol-neutral shape."""
    from apps.impact.services import semantic_datapoint_metadata

    datapoints = []
    for key, register in (template.register_map or {}).items():
        if not isinstance(register, dict):
            continue
        datapoint = {
            "key": key,
            "label": register.get("label") or key.replace("_", " ").title(),
            "address": register.get("address", 0),
            "functionCode": register.get("functionCode", 3),
            "objectsCount": register.get("objectsCount", _default_objects_count(register.get("type"))),
            "data_type": register.get("type", "uint16"),
            "access": "write" if register.get("writable") else "read",
            "scale": register.get("scale", register.get("multiplier", 1)),
            "offset": register.get("offset", 0),
            "unit": register.get("unit", ""),
            "quality": register.get("quality", {}),
            "protocol_metadata": register.get("protocol_metadata", {}),
        }
        expected_interval = template.default_polling_interval
        if team is not None:
            from apps.subscriptions.enforcement import get_latency_limit_for_team

            expected_interval = max(float(expected_interval or 5), float(get_latency_limit_for_team(team)))
        datapoint.update(
            semantic_datapoint_metadata(
                key,
                register,
                expected_interval=expected_interval,
            )
        )
        datapoints.append(datapoint)
    return datapoints


def normalized_device_datapoints(device, *, require_confirmed=True):
    """Return normalized metadata from a fixed template or a per-device map."""
    register_map = effective_register_map(device, require_confirmed=require_confirmed)
    return register_map_to_datapoints(register_map)


def human_config_preview(gateway):
    from apps.subscriptions.enforcement import get_effective_polling_interval_seconds

    connectors = generate_connector_config(gateway)
    devices = [
        device for device in gateway.devices.select_related("template").all()
        if not (device.metadata or {}).get("guided_setup_validation")
        or (device.metadata or {}).get("guided_setup_validation") == "validated"
    ]
    return {
        "gateway": gateway.name,
        "connector_count": len(connectors),
        "device_count": len(devices),
        "connectors": [
            {
                "name": connector.get("name"),
                "protocol": connector.get("type"),
                "device_count": len(connector.get("config", {}).get("master", {}).get("slaves", [])),
            }
            for connector in connectors
        ],
        "devices": [
            {
                "name": device.name,
                "template": device.template.name if device.template else "",
                "protocol": device.get_protocol_display(),
                "target": device.port or "",
                "slave_id": (device.connection_config or {}).get("slave_id"),
                "polling_interval": (get_effective_polling_interval_seconds(device) if device.template else None),
                "telemetry_keys": [datapoint["key"] for datapoint in normalized_device_datapoints(device)]
                if device.template
                else [],
            }
            for device in devices
        ],
        "dashboard": "Automatic operations dashboard",
    }


def generate_and_push_config(gateway, *, setup_run=None):
    """
    Generate connector config for a gateway and push it via MQTT.

    Returns the GatewayConfig record or None.
    """
    from .gateway_config_delivery import ensure_gateway_configurable, queue_gateway_config

    ensure_gateway_configurable(gateway)

    connectors = generate_connector_config(gateway)

    if not connectors:
        logger.info("No connectors to push for gateway %s", gateway.serial_number)
        return None

    config_payload = {
        "connectors": connectors,
    }

    gateway.lifecycle_status = "commissioning"
    gateway.save(update_fields=["lifecycle_status"])

    config_record = queue_gateway_config(
        gateway,
        "connector_update",
        config_payload,
        setup_run=setup_run,
    )
    logger.info(
        "Pushed connector config to %s: %d connectors, request_id=%s",
        gateway.serial_number,
        len(connectors),
        config_record.request_id,
    )
    return config_record
