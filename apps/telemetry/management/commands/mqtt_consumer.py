import json
import logging

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models.fields import NOT_PROVIDED
from django.utils import timezone

logger = logging.getLogger("novena_hub")
MAX_SIGNED_COMMAND_CLOCK_SKEW_SECONDS = 120

SCOPED_INBOUND_TOPICS = {
    "telemetry": "v1/gateway/+/telemetry",
    "logs": "v1/gateway/+/logs",
    "attributes": "v1/gateway/+/attributes",
    "rpc_response": "v1/gateway/+/rpc/response",
    "bootstrap_hello": "v1/gateway/+/bootstrap/hello",
}
LEGACY_SHARED_TOPICS = {
    "v1/gateway/telemetry": "telemetry",
    "v1/gateway/logs": "logs",
    "v1/gateway/attributes": "attributes",
    "v1/gateway/rpc/response": "rpc_response",
}


def heartbeat_clock_skew_seconds(payload, *, received_at=None):
    """Return absolute Gateway-to-Hub clock skew from a heartbeat timestamp."""
    try:
        gateway_timestamp = float(payload.get("ts"))
    except (AttributeError, TypeError, ValueError):
        return None
    if gateway_timestamp <= 0:
        return None
    if gateway_timestamp > 100_000_000_000:
        gateway_timestamp /= 1000
    received_at = received_at or timezone.now()
    return abs(received_at.timestamp() - gateway_timestamp)


class Command(BaseCommand):
    help = "Starts the MQTT consumer service to ingest device telemetry and edge gateway messages"

    def handle(self, *args, **options):
        import redis

        self.redis_client = redis.Redis.from_url(settings.REDIS_URL)

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=settings.MQTT_CONSUMER_CLIENT_ID
        )

        client.on_connect = self.on_connect
        client.on_message = self.on_message

        # Connect to broker
        broker_host = settings.MQTT_BROKER_HOST
        broker_port = settings.MQTT_BROKER_PORT

        self.stdout.write(self.style.SUCCESS(f"Connecting to MQTT Broker at {broker_host}:{broker_port}..."))

        try:
            client.connect(broker_host, broker_port, 60)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Could not connect to MQTT Broker: {e}"))
            return

        # Start the loop
        client.loop_forever()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self.stdout.write(self.style.SUCCESS("Connected to MQTT Broker successfully."))
            for topic in SCOPED_INBOUND_TOPICS.values():
                client.subscribe(topic)
            if getattr(settings, "MQTT_ACCEPT_LEGACY_SHARED_INBOUND", False):
                for topic in LEGACY_SHARED_TOPICS:
                    client.subscribe(topic)
            self.stdout.write(self.style.NOTICE("Subscribed to scoped gateway inbound topics"))
        else:
            self.stdout.write(self.style.ERROR(f"Connection failed with code {reason_code}"))

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            message_type, gateway = self._resolve_inbound_gateway(msg.topic, payload)
            if not message_type or not gateway:
                return

            if message_type == "telemetry":
                self._handle_telemetry(payload, gateway=gateway, topic=msg.topic)
            elif message_type == "logs":
                self._handle_logs(payload, gateway=gateway)
            elif message_type == "attributes":
                self._handle_attributes(payload, gateway=gateway)
            elif message_type == "rpc_response":
                self._handle_rpc_response(payload, gateway=gateway)
            elif message_type == "bootstrap_hello":
                self._handle_bootstrap_hello(payload, gateway=gateway)
            else:
                logger.warning("Unknown MQTT topic: %s", msg.topic)
        except Exception as e:
            logger.error("Error processing MQTT message on %s: %s", msg.topic, e, exc_info=True)

    def _resolve_inbound_gateway(self, topic, payload):
        from apps.devices.models import GatewayInventory

        parts = topic.split("/")
        message_type = None
        topic_serial = None

        if len(parts) >= 4 and parts[:2] == ["v1", "gateway"]:
            topic_serial = parts[2]
            suffix = "/".join(parts[3:])
            message_type = {
                "telemetry": "telemetry",
                "logs": "logs",
                "attributes": "attributes",
                "rpc/response": "rpc_response",
                "bootstrap/hello": "bootstrap_hello",
            }.get(suffix)

        if not message_type and topic in LEGACY_SHARED_TOPICS:
            if not getattr(settings, "MQTT_ACCEPT_LEGACY_SHARED_INBOUND", False):
                logger.warning("Rejected legacy shared MQTT topic while bridge is disabled: %s", topic)
                return None, None
            message_type = LEGACY_SHARED_TOPICS[topic]
            topic_serial = payload.get("serial_number")
            logger.warning(
                "Accepted legacy shared MQTT topic %s using payload serial %s. Enable only during Gateway migration.",
                topic,
                topic_serial or "missing",
            )

        if not message_type:
            logger.warning("Unknown MQTT topic: %s", topic)
            return None, None

        payload_serial = payload.get("serial_number")
        if topic_serial and payload_serial and payload_serial != topic_serial:
            logger.warning(
                "Rejected MQTT %s from topic serial %s with payload serial %s.",
                message_type,
                topic_serial,
                payload_serial,
            )
            return None, None

        if not topic_serial:
            logger.warning("Rejected MQTT %s on %s because no gateway serial was identified.", message_type, topic)
            return None, None

        inventory = (
            GatewayInventory.objects.select_related("gateway")
            .filter(serial_number=topic_serial, status="claimed", gateway__isnull=False)
            .first()
        )
        gateway = inventory.gateway if inventory else None
        if not gateway or gateway.lifecycle_status in {"release_pending", "released"}:
            logger.warning("Rejected MQTT %s for unknown gateway serial %s.", message_type, topic_serial)
            return None, None

        return message_type, gateway

    # ── Telemetry (Ingestion queueing and WebSockets broadcast) ──────────

    def _handle_telemetry(self, payload, gateway=None, topic="v1/gateway/telemetry"):
        """Queue telemetry data and broadcast it to the browser live stream."""
        cloud_received_at = timezone.now()
        trusted_gateway_sn = gateway.serial_number if gateway else None

        try:
            queued_payload = dict(payload)
            queued_payload["_cloud_received_at"] = cloud_received_at.isoformat()
            if trusted_gateway_sn:
                queued_payload["_topic_gateway_sn"] = trusted_gateway_sn
                queued_payload["_topic_gateway_id"] = gateway.pk
            self.redis_client.rpush("telemetry_ingest_queue", json.dumps(queued_payload))
        except Exception as e:
            logger.error("Failed to queue telemetry raw payload to Redis: %s", e)

        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            from apps.devices.models import Device
            from apps.telemetry.mqtt_parser import parse_mqtt_payload
            from apps.utils.timezones import format_site_datetime, site_timezone_metadata

            events = parse_mqtt_payload(topic, payload, trusted_gateway_sn=trusted_gateway_sn)
            channel_layer = get_channel_layer()

            gateway_cache = {gateway.serial_number: gateway} if gateway else {}
            device_cache = {}
            device_by_id = {}

            for event in events:
                gateway_sn = event.get("gateway_sn")
                device_name = event.get("device_name")
                device_id = event.get("device_id")
                values = dict(event.get("values", {}))
                timestamp = event.get("timestamp") or cloud_received_at

                if device_name and "device_name" not in values:
                    values["device_name"] = device_name

                if not gateway_sn:
                    continue

                if gateway_sn not in gateway_cache:
                    from apps.devices.services import current_claimed_gateway

                    resolved = current_claimed_gateway(gateway_sn)
                    if not resolved:
                        continue
                    gateway_cache[gateway_sn] = resolved
                gateway = gateway_cache[gateway_sn]

                target_device = None
                if device_id:
                    try:
                        d_id = int(device_id)
                        if d_id not in device_by_id:
                            device_by_id[d_id] = Device.objects.filter(id=d_id, gateway=gateway).first()
                        target_device = device_by_id[d_id]
                    except (ValueError, TypeError):
                        logger.warning("Invalid telemetry device_id %s from gateway %s.", device_id, gateway_sn)

                if not target_device and device_name:
                    cache_key = (gateway.id, device_name)
                    if cache_key not in device_cache:
                        device_cache[cache_key] = Device.objects.filter(gateway=gateway, name=device_name).first()
                    target_device = device_cache[cache_key]

                if not target_device:
                    legacy_candidates = list(Device.objects.filter(gateway=gateway).order_by("id")[:2])
                    if len(legacy_candidates) == 1:
                        target_device = legacy_candidates[0]
                        logger.warning(
                            "Using legacy first-device telemetry fallback for gateway %s (single configured device). "
                            "Payload device_id=%s device_name=%s resolved_device=%s.",
                            gateway_sn,
                            device_id,
                            device_name,
                            target_device.id,
                        )
                    elif len(legacy_candidates) > 1:
                        logger.warning(
                            "Rejected ambiguous live telemetry for gateway %s. "
                            "Payload device_id=%s device_name=%s; the gateway has multiple configured devices.",
                            gateway_sn,
                            device_id,
                            device_name,
                        )

                if target_device:
                    timezone_data = site_timezone_metadata(target_device.site)
                    async_to_sync(channel_layer.group_send)(
                        f"device_{target_device.id}",
                        {
                            "type": "telemetry_message",
                            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
                            "timestamp_local": format_site_datetime(timestamp, target_device.site),
                            **timezone_data,
                            "values": values,
                        },
                    )
        except Exception as e:
            logger.error("Error broadcasting WebSocket telemetry: %s", e, exc_info=True)

    # ── Remote Logging ──────────────────────────────────────────────────

    def _handle_logs(self, payload, gateway=None):
        """Queue log entries from a gateway to Redis."""
        try:
            queued_payload = dict(payload)
            if gateway:
                queued_payload["_topic_gateway_sn"] = gateway.serial_number
                queued_payload["_topic_gateway_id"] = gateway.pk
            self.redis_client.rpush("logs_ingest_queue", json.dumps(queued_payload))
        except Exception as e:
            logger.error(f"Failed to queue logs raw payload to Redis: {e}")

    # ── Attribute Sync / Heartbeat ──────────────────────────────────────

    def _handle_bootstrap_hello(self, payload, gateway=None):
        """Mark a released/unclaimed gateway as visible in bootstrap mode."""
        from apps.devices.activation import reissue_activation_for_gateway
        from apps.devices.services import current_claimed_gateway

        gateway_sn = gateway.serial_number if gateway else payload.get("serial_number")
        if not gateway_sn:
            return
        current_gateway = current_claimed_gateway(gateway_sn)
        gateway = None if gateway and (not current_gateway or current_gateway.pk != gateway.pk) else current_gateway
        if not gateway:
            logger.info("Bootstrap hello from unknown gateway %s", gateway_sn)
            return
        gateway.last_bootstrap_seen_at = timezone.now()
        if gateway.lifecycle_status == "claimed":
            gateway.lifecycle_status = "bootstrap_seen"
            gateway.save(update_fields=["last_bootstrap_seen_at", "lifecycle_status"])
        else:
            gateway.save(update_fields=["last_bootstrap_seen_at"])
        try:
            reissue_activation_for_gateway(gateway)
        except ValueError as exc:
            logger.warning("Rejected activation reissue for %s: %s", gateway_sn, exc)

    def _handle_attributes(self, payload, gateway=None):
        """Update gateway status from heartbeat attributes or LWT."""
        from apps.devices.models import GatewayConfig
        from apps.devices.services import current_claimed_gateway

        gateway_sn = gateway.serial_number if gateway else payload.get("serial_number")
        if not gateway_sn:
            return

        if not gateway:
            gateway = current_claimed_gateway(gateway_sn)
            if not gateway:
                logger.warning("Gateway %s not found for attribute sync", gateway_sn)
                return

        attrs = payload.get("attributes", {})
        previously_clock_ready = gateway.remote_control_clock_ready

        # Update gateway fields
        update_fields = ["last_seen"]
        gateway.last_seen = timezone.now()

        field_mapping = {
            "status": "status",
            "firmware_version": "firmware_version",
            "ip_address": "ip_address",
            "uptime_seconds": "uptime_seconds",
            "python_version": "python_version",
            "platform": "platform_info",
            "connected_devices": "connected_devices",
            "active_connectors": "active_connectors",
            "gateway_capabilities": "gateway_capabilities",
            "remote_control_protocol_version": "remote_control_protocol_version",
            "remote_control_capabilities": "remote_control_capabilities",
            "remote_control_local_writeback_enabled": "remote_control_local_writeback_enabled",
            "remote_control_policy_loaded": "remote_control_policy_loaded",
            "remote_control_policy_revision": "remote_control_policy_revision",
            "remote_control_epoch": "remote_control_epoch",
            "remote_control_clock_ready": "remote_control_clock_ready",
            "remote_control_journal_ready": "remote_control_journal_ready",
            "remote_control_event_spool_count": "remote_control_event_spool_count",
            "remote_control_storage_healthy": "remote_control_storage_healthy",
            "active_interface": "active_interface",
            "failover_count": "failover_count",
            "ethernet_status": "ethernet_status",
            "wifi_status": "wifi_status",
            "fourg_status": "fourg_status",
            "signal_strength": "signal_strength",
            "buffered_event_count": "buffered_event_count",
            "last_replay_status": "last_replay_status",
            "replay_failure_count": "replay_failure_count",
            "connectivity_checked_ts": "connectivity_checked_ts",
            "internet_reachable": "internet_reachable",
            "default_route_ok": "default_route_ok",
            "default_route_error": "default_route_error",
            "dns_ok": "dns_ok",
            "dns_error": "dns_error",
            "broker_host": "broker_host",
            "broker_port": "broker_port",
            "broker_tcp_ok": "broker_tcp_ok",
            "broker_tcp_error": "broker_tcp_error",
            "tls_ok": "tls_ok",
            "tls_error": "tls_error",
            "mqtt_connected": "mqtt_connected",
            "mqtt_last_error": "mqtt_last_error",
            "device_health": "device_health",
            "ota_status": "ota_status",
            "ota_version": "ota_version",
            "ota_error": "ota_error",
            "ota_rollback_performed": "ota_rollback_performed",
        }

        for attr_key, model_field in field_mapping.items():
            if attr_key in attrs:
                value = self._normalize_gateway_attribute_value(gateway, model_field, attrs[attr_key])
                if value is None:
                    continue
                setattr(gateway, model_field, value)
                update_fields.append(model_field)

        clock_skew = heartbeat_clock_skew_seconds(payload)
        if clock_skew is not None and clock_skew > MAX_SIGNED_COMMAND_CLOCK_SKEW_SECONDS:
            gateway.remote_control_clock_ready = False
            gateway.gateway_capabilities = [
                capability
                for capability in (gateway.gateway_capabilities or [])
                if capability != "guided_setup_v1"
            ]
            update_fields.extend(["remote_control_clock_ready", "gateway_capabilities"])
            if previously_clock_ready or attrs.get("remote_control_clock_ready"):
                logger.warning(
                    "Gateway %s clock differs from Hub by %.0f seconds; signed setup commands are blocked.",
                    gateway.serial_number,
                    clock_skew,
                )

        if attrs.get("status") == "online" and gateway.lifecycle_status == "claimed":
            gateway.lifecycle_status = "online"
            update_fields.append("lifecycle_status")

        gateway.save(update_fields=list(dict.fromkeys(update_fields)))

        policy_ack_revision = attrs.get("remote_control_policy_ack_revision")
        policy_ack_epoch = attrs.get("remote_control_policy_ack_epoch")
        if policy_ack_revision is not None and policy_ack_epoch is not None:
            from apps.devices.models import GatewayControlPolicyBundle

            acknowledged = GatewayControlPolicyBundle.objects.filter(
                gateway=gateway,
                revision=policy_ack_revision,
                control_epoch=policy_ack_epoch,
            ).first()
            if acknowledged:
                GatewayControlPolicyBundle.objects.filter(gateway=gateway, is_active=True).exclude(
                    pk=acknowledged.pk
                ).update(is_active=False)
                acknowledged.acknowledged_at = timezone.now()
                acknowledged.is_active = True
                acknowledged.save(update_fields=["acknowledged_at", "is_active", "updated_at"])

        reconciliation = attrs.get("remote_control_reconciliation")
        if isinstance(reconciliation, list):
            from apps.devices.models import RemoteCommand
            from apps.devices.remote_control import transition_command

            for edge_event in reconciliation:
                command = RemoteCommand.objects.filter(
                    pk=edge_event.get("command_id"),
                    gateway=gateway,
                ).first()
                if not command:
                    continue
                if command.events.filter(
                    event_type="gateway_journal_reconciled",
                    evidence=edge_event,
                ).exists():
                    continue
                if edge_event.get("status") == "success" and edge_event.get("stage") == "verified":
                    target_status = RemoteCommand.Status.RECONCILED_VERIFIED
                elif edge_event.get("stage") == "rejected":
                    target_status = RemoteCommand.Status.RECONCILED_NOT_APPLIED
                else:
                    target_status = RemoteCommand.Status.RECONCILED_UNRESOLVED
                transition_command(
                    command,
                    target_status,
                    "gateway_journal_reconciled",
                    evidence=edge_event,
                )

        # Handle discovery report from Edge auto-scan
        discovery_report = attrs.get("discovery_report")
        if discovery_report:
            self._process_discovery_report(gateway, discovery_report)

        # Handle config update acknowledgement
        config_request_id = attrs.get("config_update_request_id")
        if config_request_id:
            try:
                from apps.devices.deployment_setup import customer_safe_error
                from apps.devices.gateway_config_delivery import acknowledge_gateway_config

                config_record = acknowledge_gateway_config(gateway, attrs)
                if config_record.technical_error:
                    config_record.error_message = customer_safe_error(config_record.technical_error)
                    config_record.save(update_fields=["error_message", "updated_at"])
                if config_record.status == "active":
                    gateway.lifecycle_status = "active"
                    gateway.save(update_fields=["lifecycle_status"])
                logger.info("Config update %s acknowledged: %s", config_request_id, config_record.status)
            except (GatewayConfig.DoesNotExist, TypeError, ValueError) as exc:
                logger.warning("Rejected config acknowledgement %s: %s", config_request_id, exc)

        credential_status = attrs.get("credential_update_status")
        if credential_status:
            credential_action = attrs.get("credential_update_action")
            credential_request_id = attrs.get("credential_update_request_id")
            if credential_action == "activate" and credential_request_id:
                from apps.devices.activation import acknowledge_gateway_activation

                activation = acknowledge_gateway_activation(
                    gateway,
                    credential_request_id,
                    attrs.get("credential_update_generation"),
                    credential_status,
                    attrs.get("credential_update_error", "") or "",
                )
                if activation and credential_status == "success":
                    gateway.credential_rotation_status = "success"
                    if gateway.lifecycle_status in ("claimed", "bootstrap_seen", "activating"):
                        gateway.lifecycle_status = "online"
                    gateway.save(update_fields=["credential_rotation_status", "lifecycle_status"])
                elif activation:
                    gateway.credential_rotation_status = credential_status
                    gateway.save(update_fields=["credential_rotation_status"])
            else:
                gateway.credential_rotation_status = credential_status
                gateway.save(update_fields=["credential_rotation_status"])

        logger.debug("Updated attributes for gateway %s (status=%s)", gateway_sn, attrs.get("status"))

    def _normalize_gateway_attribute_value(self, gateway, model_field, value):
        if value is not None:
            return value
        field = gateway._meta.get_field(model_field)
        if field.null:
            return None
        if field.get_internal_type() in {"CharField", "TextField"}:
            return ""
        if field.default is not NOT_PROVIDED:
            return field.get_default()
        return None

    # ── RPC Response ────────────────────────────────────────────────────

    def _handle_rpc_response(self, payload, gateway=None):
        """Process RPC command response from a gateway."""
        from apps.devices.models import RpcCommand
        from apps.devices.remote_control import append_command_event, transition_command
        from apps.devices.remote_control_protocol import GATEWAY_STAGE_TO_COMMAND_STATUS

        request_id = payload.get("request_id")
        if not request_id:
            return

        try:
            if gateway:
                rpc_record = RpcCommand.objects.get(request_id=request_id, gateway=gateway)
            else:
                rpc_record = RpcCommand.objects.get(request_id=request_id)
            response_status = payload.get("status", "unknown")
            response_stage = payload.get("stage", response_status)
            method = payload.get("method")
            if method and method != rpc_record.method:
                logger.warning("Rejected mismatched RPC method for request_id %s", request_id)
                return
            allowed_stage = response_stage in GATEWAY_STAGE_TO_COMMAND_STATUS
            if response_stage in {"field_protocol_accepted", "field_execution_verified"}:
                allowed_stage = rpc_record.method == "write_device"
            elif response_stage == "ota_initiated":
                allowed_stage = rpc_record.method == "update_firmware"
            elif response_stage == "gateway_action_completed":
                allowed_stage = rpc_record.method not in {"write_device", "update_firmware"}

            if rpc_record.remote_command_id and allowed_stage:
                target = GATEWAY_STAGE_TO_COMMAND_STATUS[response_stage]
                updates = {"execution_status": response_stage}
                if response_stage == "gateway_received":
                    updates["gateway_received_at"] = timezone.now()
                transition_command(
                    rpc_record.remote_command_id,
                    target,
                    response_stage,
                    evidence={"request_id": str(request_id), "status": response_status},
                    updates=updates,
                )
            if response_status in {"received", "processing"}:
                return
            rpc_record.status = response_status
            rpc_record.response_stage = response_stage if allowed_stage else ""
            rpc_record.result = payload.get("result")
            rpc_record.error_message = payload.get("error", "") or ""
            rpc_record.responded_at = timezone.now()
            rpc_record.save(
                update_fields=[
                    "status",
                    "response_stage",
                    "result",
                    "error_message",
                    "responded_at",
                    "updated_at",
                ]
            )
            from apps.devices.services import sync_device_command_from_rpc

            sync_device_command_from_rpc(rpc_record)
            verification = (rpc_record.result or {}).get("verification", {})
            if (
                rpc_record.remote_command_id
                and verification.get("status") == "mismatch"
                and rpc_record.remote_command.device_id
            ):
                from apps.devices.models import ControlActivation, RemoteControlScope

                command = rpc_record.remote_command
                reason = "Automatic suspension after post-write verification mismatch"
                RemoteControlScope.objects.filter(
                    team=command.team,
                    device=command.device,
                    command_key=command.command_key,
                ).update(mode=RemoteControlScope.Mode.SUSPENDED, reason=reason)
                ControlActivation.objects.filter(
                    team=command.team,
                    device=command.device,
                    command_key=command.command_key,
                    status=ControlActivation.Status.ACTIVE,
                ).update(status=ControlActivation.Status.SUSPENDED, suspended_reason=reason)
                append_command_event(
                    command,
                    "control_key_auto_suspended",
                    evidence=verification,
                )
            logger.info(
                "RPC response for %s (%s): %s",
                request_id,
                payload.get("method"),
                rpc_record.status,
            )
        except RpcCommand.DoesNotExist:
            logger.warning("RPC response for unknown request_id: %s", request_id)

    # ── Discovery Report Processing ─────────────────────────────────────

    def _process_discovery_report(self, gateway, report):
        """
        Process a discovery report from the Edge gateway.
        Stores it in Gateway.discovery_data and auto-matches against DeviceTemplates.
        """
        if not isinstance(report, dict):
            logger.warning("Rejected malformed discovery report for %s", gateway.serial_number)
            return
        current = gateway.discovery_data or {}
        try:
            incoming_ts = int(report.get("scan_ts") or 0)
            current_ts = int(current.get("scan_ts") or 0)
        except (TypeError, ValueError):
            logger.warning("Rejected discovery report with invalid timestamp for %s", gateway.serial_number)
            return
        incoming_scan_id = str(report.get("scan_id") or "")
        current_scan_id = str(current.get("scan_id") or "")
        active_scan_id = ""
        if not incoming_scan_id and report.get("scan_type") == "guided":
            active_scan_id = self._active_guided_setup_scan_id(gateway)
            if active_scan_id:
                report = {**report, "scan_id": active_scan_id}
                incoming_scan_id = active_scan_id
                logger.info(
                    "Backfilled missing guided discovery scan_id for %s from active setup run: %s",
                    gateway.serial_number,
                    active_scan_id,
                )
        if not active_scan_id:
            active_scan_id = self._active_guided_setup_scan_id(gateway)
        belongs_to_active_scan = bool(active_scan_id and incoming_scan_id == active_scan_id)
        if current_ts and incoming_ts and incoming_ts < current_ts and not belongs_to_active_scan:
            logger.info(
                "Ignored stale discovery report for %s (scan_ts=%s, current=%s)",
                gateway.serial_number,
                incoming_ts,
                current_ts,
            )
            return
        if current_ts and not incoming_ts:
            logger.info("Ignored unversioned discovery report for %s", gateway.serial_number)
            return
        if incoming_scan_id and incoming_scan_id == current_scan_id and incoming_ts == current_ts:
            terminal = {"complete", "cancelled", "error"}
            if current.get("status") in terminal and report.get("status") not in terminal:
                logger.info("Ignored non-terminal discovery regression for %s", gateway.serial_number)
                return
            try:
                incoming_updated = int(report.get("updated_at") or 0)
                current_updated = int(current.get("updated_at") or 0)
            except (TypeError, ValueError):
                logger.warning("Rejected discovery report with invalid update timestamp for %s", gateway.serial_number)
                return
            if incoming_updated and current_updated and incoming_updated < current_updated:
                logger.info("Ignored out-of-order discovery progress for %s", gateway.serial_number)
                return

        discovered_devices = report.get("discovered_devices", [])
        if not isinstance(discovered_devices, list):
            logger.warning("Rejected discovery report with invalid device list for %s", gateway.serial_number)
            return
        from apps.devices.discovery_matching import enrich_discovered_device

        discovered_devices = [enrich_discovered_device(device, team=gateway.team) for device in discovered_devices]

        # Store enriched discovery data
        gateway.discovery_data = {
            "schema_version": report.get("schema_version", 0),
            "scan_id": report.get("scan_id", ""),
            "last_discovered_at": str(timezone.now()),
            "scan_ts": report.get("scan_ts"),
            "started_at": report.get("started_at"),
            "updated_at": report.get("updated_at"),
            "received_at": timezone.now().isoformat(),
            "completed_at": report.get("completed_at"),
            "scan_type": report.get("scan_type", "unknown"),
            "status": report.get("status", "complete"),
            "phase": report.get("phase", ""),
            "progress": report.get("progress", {}),
            "interfaces": report.get("interfaces", []),
            "devices": discovered_devices,
            "skipped_configured": report.get("skipped_configured", []),
            "errors": report.get("errors", []),
        }
        gateway.save(update_fields=["discovery_data"])

        logger.info(
            "Discovery report processed for %s: %d devices found, %d matched",
            gateway.serial_number,
            len(discovered_devices),
            sum(1 for d in discovered_devices if d.get("matched_template_id")),
        )

    def _active_guided_setup_scan_id(self, gateway):
        from apps.devices.models import DeploymentSetupRun

        run = (
            DeploymentSetupRun.objects.filter(
                gateway=gateway,
                state=DeploymentSetupRun.State.DISCOVERING,
            )
            .order_by("-updated_at")
            .first()
        )
        if not run:
            return ""
        return str(((run.summary or {}).get("discovery") or {}).get("active_scan_id") or "")
