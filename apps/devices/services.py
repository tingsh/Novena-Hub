import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import DeviceCommand, DeviceTemplate, Gateway, GatewayInventory, Site

logger = logging.getLogger("novena_hub")


def compute_claim_code(serial_number: str) -> str:
    """
    Derive a deterministic claim code from a gateway serial number using HMAC-SHA256.
    The claim code is printed on the gateway sticker and used as the initial MQTT password.
    """
    return (
        hmac.new(
            settings.GATEWAY_CLAIM_SECRET.encode(),
            serial_number.strip().upper().encode(),
            hashlib.sha256,
        )
        .hexdigest()[:8]
        .upper()
    )


def validate_claim_code(serial_number: str, claim_code: str) -> bool:
    """
    Validate a claim code against a serial number. Pure HMAC check — no DB lookup needed.
    """
    expected = compute_claim_code(serial_number)
    return hmac.compare_digest(expected, claim_code.strip().upper())


class GatewayClaimError(ValueError):
    """Raised when a gateway claim cannot be completed safely."""


def sites_for_team(team):
    """Sites a request may reference for tenant-scoped device/gateway mutations."""
    return Site.objects.filter(team=team)


def gateways_for_team(team):
    """Gateways a request may reference for tenant-scoped device mutations."""
    return Gateway.objects.filter(team=team)


def visible_templates_for_team(team):
    """Global templates plus private templates created by the current team."""
    return DeviceTemplate.objects.filter(Q(created_by_team__isnull=True) | Q(created_by_team=team))


def normalize_gateway_serial(serial_number: str) -> str:
    return serial_number.strip().upper()


def current_claimed_gateway(serial_number: str):
    """Resolve the one factory-authorized ownership row for MQTT/edge ingress."""
    serial_number = normalize_gateway_serial(serial_number)
    inventory = (
        GatewayInventory.objects.select_related("gateway")
        .filter(serial_number=serial_number, status="claimed", gateway__isnull=False)
        .first()
    )
    gateway = inventory.gateway if inventory else None
    if not gateway or gateway.lifecycle_status in {"release_pending", "released"}:
        return None
    return gateway


def validate_gateway_claim(serial_number: str, claim_code: str):
    """Validate a sticker claim against factory inventory and HMAC claim code."""
    serial_number = normalize_gateway_serial(serial_number)
    if not validate_claim_code(serial_number, claim_code):
        raise GatewayClaimError("Invalid claim code. Please check the sticker on the bottom of your gateway.")

    inventory = GatewayInventory.objects.filter(serial_number__iexact=serial_number).first()
    existing_gateway = Gateway.objects.exclude(lifecycle_status="released").filter(serial_number=serial_number).first()
    if not inventory and not existing_gateway:
        raise GatewayClaimError("This serial number is not in the Novena factory inventory. Please contact support.")
    if inventory and inventory.status == "retired":
        raise GatewayClaimError("This gateway has been retired and cannot be claimed.")
    return inventory


def generate_operational_mqtt_password() -> str:
    """Create the random credential the gateway uses after claim activation."""
    return secrets.token_urlsafe(32)


@transaction.atomic
def claim_gateway_for_team(team, site, name: str, serial_number: str, claim_code: str):
    """Bind a manufactured gateway to a customer team/site after sticker validation."""
    serial_number = normalize_gateway_serial(serial_number)
    inventory = validate_gateway_claim(serial_number, claim_code)
    operational_password = generate_operational_mqtt_password()

    if inventory:
        inventory = GatewayInventory.objects.select_for_update().get(pk=inventory.pk)
    existing_gateway = None
    if inventory and inventory.gateway_id:
        existing_gateway = Gateway.objects.select_for_update().filter(pk=inventory.gateway_id).first()
    if not existing_gateway:
        existing_gateway = (
            Gateway.objects.select_for_update()
            .exclude(lifecycle_status="released")
            .filter(serial_number=serial_number)
            .first()
        )
    if existing_gateway and existing_gateway.team != team:
        raise GatewayClaimError(
            "This serial number is already registered to another team. Please contact support if this is an error."
        )
    is_new_for_team = (
        not Gateway.objects.filter(
            team=team,
            serial_number=serial_number,
        )
        .exclude(lifecycle_status="released")
        .exists()
    )
    if not existing_gateway and is_new_for_team:
        from apps.subscriptions.enforcement import can_add_gateway, get_gateway_limit_for_team

        if not can_add_gateway(team):
            limit = get_gateway_limit_for_team(team)
            raise GatewayClaimError(
                f"Your current plan supports up to {limit} gateway{'s' if limit != 1 else ''}. "
                "Upgrade your plan or release an unused gateway before adding another."
            )

    if inventory and inventory.status == "claimed" and inventory.claimed_by_team and inventory.claimed_by_team != team:
        raise GatewayClaimError(
            "This serial number is already registered to another team. Please contact support if this is an error."
        )

    if existing_gateway:
        gateway = existing_gateway
        gateway.site = site
        gateway.name = name
        gateway.mqtt_username = serial_number
        gateway.mqtt_password = ""
        gateway.mqtt_provisioning_status = "pending"
        gateway.mqtt_provisioning_error = ""
        gateway.credential_rotation_status = "pending"
        if gateway.status == "online":
            gateway.lifecycle_status = "activating"
        elif gateway.lifecycle_status not in ("commissioning", "active"):
            gateway.lifecycle_status = "claimed"
        gateway.save(
            update_fields=[
                "site",
                "name",
                "mqtt_username",
                "mqtt_password",
                "mqtt_provisioning_status",
                "mqtt_provisioning_error",
                "credential_rotation_status",
                "lifecycle_status",
            ]
        )
    else:
        gateway = Gateway.objects.create(
            team=team,
            site=site,
            name=name,
            serial_number=serial_number,
            access_token=secrets.token_hex(20),
            mqtt_username=serial_number,
            mqtt_password="",
            mqtt_provisioning_status="pending",
            credential_rotation_status="pending",
            lifecycle_status="claimed",
        )

    if inventory:
        inventory.status = "claimed"
        inventory.claimed_by_team = team
        inventory.gateway = gateway
        if not inventory.claimed_at:
            inventory.claimed_at = timezone.now()
        inventory.save(update_fields=["status", "claimed_by_team", "gateway", "claimed_at"])

    from .activation import queue_gateway_activation

    queue_gateway_activation(gateway, operational_password)

    return gateway


@transaction.atomic
def release_gateway_for_redo(gateway):
    """
    Release a gateway for customer self-serve onboarding redo.

    The Gateway row is kept so the physical device can fall back to bootstrap mode
    and be re-claimed with the same serial number + printed claim code.
    """
    from .gateway_release import request_gateway_release

    request_gateway_release(gateway)
    return Gateway.objects.get(pk=gateway.pk)


READ_FUNCTION_CODES = {1, 2, 3, 4}
WRITE_FUNCTION_CODES = {5, 6, 15, 16}


def _normalize_command_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        with contextlib.suppress(ValueError):
            return float(value)
    return value


def _register_params_from_template(device, key, command_type, value):
    from .datapoint_maps import effective_register_map

    register = effective_register_map(device).get(key)

    if not register or "address" not in register:
        raise ValueError(f"No mapped register entry found for '{key}'.")

    params = {
        "address": register["address"],
        "functionCode": register.get("functionCode", 6 if command_type == "write" else 3),
    }
    if register.get("type") and register["type"] != "bool":
        params["type"] = register["type"]
    if "objectsCount" in register:
        params["objectsCount"] = register["objectsCount"]
    elif command_type == "read":
        params["objectsCount"] = 1
    if command_type == "write":
        params["value"] = value
    return params


def _validate_device_command_params(command_type, params):
    function_code = params.get("functionCode")
    if function_code is None:
        raise ValueError("Missing functionCode.")
    function_code = int(function_code)
    params["functionCode"] = function_code

    if "address" not in params:
        raise ValueError("Missing register address.")
    params["address"] = int(params["address"])

    if command_type == "write":
        if function_code not in WRITE_FUNCTION_CODES:
            raise ValueError("Write commands must use function code 5, 6, 15, or 16.")
        if "value" not in params:
            raise ValueError("Write commands require a value.")
        params["value"] = _normalize_command_value(params["value"])
    elif command_type == "read":
        if function_code not in READ_FUNCTION_CODES:
            raise ValueError("Read commands must use function code 1, 2, 3, or 4.")
        params["objectsCount"] = int(params.get("objectsCount", 1))
    else:
        raise ValueError("Command type must be 'read' or 'write'.")


def send_device_command(device, user, key, value=None, *, command_type="write", params=None):
    """
    Sends a customer-facing command to a device via the associated gateway.
    DeviceCommand is the customer audit record; RpcCommand is the transport record.
    """
    from .remote_control import request_remote_command, resolve_device_params
    from .remote_control_protocol import canonical_device_operation

    if not device.gateway:
        raise ValueError("Device is not assigned to a gateway.")
    operation = canonical_device_operation(command_type)

    if not key:
        raise ValueError("A mapped canonical device command key is required.")
    command_key = key
    if params:
        raise ValueError("Raw register parameters are not accepted; select a mapped device key.")
    value = _normalize_command_value(value)
    # Reject unknown/unmapped keys before building any transport target.
    resolve_device_params(device, command_key, operation, value)
    command_type = "write" if operation == "write_device" else "read"
    transaction_id = str(uuid.uuid4())
    remote_command = request_remote_command(
        gateway=device.gateway,
        operation=operation,
        requested_by=user,
        device=device,
        command_key=command_key,
        value=value,
    )
    command = DeviceCommand.objects.create(
        team=device.team,
        device=device,
        created_by=user,
        command_type=command_type,
        command_key=command_key,
        value=remote_command.normalized_value if command_type == "write" else None,
        transaction_id=transaction_id,
        payload=remote_command.request_payload,
        remote_command=remote_command,
        status="pending",
    )
    logger.info(
        "%s command %s queued for %s (tx: %s, governed command: %s)",
        command_type.title(),
        command_key,
        device.name,
        transaction_id,
        remote_command.pk,
    )
    return command


def sync_device_command_from_rpc(rpc_record):
    """Mirror a transport-level RpcCommand result onto its customer-facing DeviceCommand."""
    command = None
    try:
        command = rpc_record.device_command
    except DeviceCommand.DoesNotExist:
        if rpc_record.remote_command_id:
            with contextlib.suppress(DeviceCommand.DoesNotExist):
                command = rpc_record.remote_command.legacy_device_command

    if rpc_record.remote_command_id:
        from .models import RemoteCommand
        from .remote_control import transition_command
        from .remote_control_protocol import GATEWAY_STAGE_TO_COMMAND_STATUS

        governed = rpc_record.remote_command
        response_payload = {
            "status": rpc_record.status,
            "stage": rpc_record.response_stage,
            "result": rpc_record.result,
            "error": rpc_record.error_message or "",
        }
        target_status = GATEWAY_STAGE_TO_COMMAND_STATUS.get(rpc_record.response_stage)
        error_code = ""
        error_message = ""
        if rpc_record.status == "timeout":
            target_status = RemoteCommand.Status.OUTCOME_UNKNOWN
            error_code = "gateway_timeout"
            error_message = rpc_record.error_message or "Timed out waiting for Gateway response."
        elif rpc_record.status == "error" and not target_status:
            target_status = RemoteCommand.Status.FAILED
            error_code = "gateway_failed"
            error_message = rpc_record.error_message or "Gateway command failed."
        elif target_status in {RemoteCommand.Status.REJECTED, RemoteCommand.Status.FAILED}:
            error_code = "gateway_rejected" if target_status == RemoteCommand.Status.REJECTED else "gateway_failed"
            error_message = rpc_record.error_message or "Gateway command failed."
        updates = {
            "response_payload": response_payload,
            "error_code": error_code,
            "error_message": error_message,
        }
        if target_status in {
            RemoteCommand.Status.VERIFIED,
            RemoteCommand.Status.ACTION_COMPLETED,
            RemoteCommand.Status.REJECTED,
            RemoteCommand.Status.FAILED,
        }:
            updates["completed_at"] = rpc_record.responded_at or timezone.now()
        if target_status:
            transition_command(
                governed,
                target_status,
                f"gateway_{rpc_record.response_stage or rpc_record.status}",
                evidence=response_payload,
                updates=updates,
            )
        else:
            RemoteCommand.objects.filter(pk=governed.pk).update(**updates)
        governed.response_payload = response_payload

    if not command:
        return None

    command.response_payload = {
        "status": rpc_record.status,
        "stage": rpc_record.response_stage,
        "result": rpc_record.result,
        "error": rpc_record.error_message or "",
    }
    if rpc_record.status == "success" and rpc_record.response_stage in {
        "field_execution_verified",
        "gateway_action_completed",
        "diagnostic_completed",
    }:
        command.status = "executed"
        command.error_message = ""
        command.executed_at = rpc_record.responded_at or timezone.now()
    elif rpc_record.status == "success" and rpc_record.response_stage in {
        "field_protocol_accepted",
        "ota_initiated",
    }:
        command.status = "accepted"
        command.error_message = ""
    elif rpc_record.status == "timeout":
        command.status = "timed_out"
        command.error_message = rpc_record.error_message or "Timed out waiting for gateway response."
    elif rpc_record.status == "error":
        command.status = "failed"
        command.error_message = rpc_record.error_message or "Gateway command failed."
        command.executed_at = rpc_record.responded_at or timezone.now()
    else:
        return command

    command.save(update_fields=["status", "response_payload", "error_message", "executed_at", "updated_at"])
    return command


def process_command_response(payload_str):
    """
    Processes an incoming command response.
    Supports the current gateway RPC response shape and the legacy id/data shape.
    """
    try:
        payload = json.loads(payload_str)
        request_id = payload.get("request_id")
        if request_id:
            from .models import RpcCommand

            rpc = RpcCommand.objects.filter(request_id=request_id).first()
            if not rpc:
                return
            rpc.status = payload.get("status", "error")
            from .remote_control_protocol import GATEWAY_STAGE_TO_COMMAND_STATUS

            stage = payload.get("stage", "")
            rpc.response_stage = stage if stage in GATEWAY_STAGE_TO_COMMAND_STATUS else ""
            rpc.result = payload.get("result")
            rpc.error_message = payload.get("error", "") or ""
            rpc.responded_at = timezone.now()
            rpc.save(
                update_fields=[
                    "status",
                    "response_stage",
                    "result",
                    "error_message",
                    "responded_at",
                    "updated_at",
                ]
            )
            sync_device_command_from_rpc(rpc)
            return

        tx_id = payload.get("id")

        if not tx_id:
            return

        command = DeviceCommand.objects.filter(transaction_id=tx_id).first()
        if command:
            command.response_payload = payload
            command.executed_at = timezone.now()

            data = payload.get("data", {})
            if data.get("success") is True or data.get("status") == "OK":
                command.status = "executed"
            else:
                command.status = "failed"
                command.error_message = data.get("error", "Execution failed at edge")

            command.save()
            logger.info(f"Command {command.transaction_id} updated to {command.status}")
    except Exception as e:
        logger.error(f"Error processing command response: {e}")


COMMISSIONING_STAGES = [
    ("site_created", "Site created"),
    ("gateway_claimed", "Gateway claimed"),
    ("gateway_connected", "Gateway connected"),
    ("device_scan_running", "Equipment scan running"),
    ("devices_discovered", "Equipment discovered"),
    ("templates_selected", "Templates selected"),
    ("config_pushed", "Config pushed to gateway"),
    ("first_telemetry_received", "First telemetry received"),
    ("dashboard_ready", "Dashboard ready"),
]


def _session_value(session, key):
    if not session:
        return None
    try:
        return session.get(key)
    except AttributeError:
        return None


def _gateway_from_context(team, gateway=None, session=None):
    if gateway:
        return gateway
    gateway_id = _session_value(session, "onboarding_gateway_id")
    if gateway_id:
        return Gateway.objects.filter(id=gateway_id, team=team).select_related("site").first()
    return Gateway.objects.filter(team=team).select_related("site").order_by("-created_at").first()


def _site_from_context(team, gateway=None, session=None):
    if gateway:
        return gateway.site
    site_id = _session_value(session, "onboarding_site_id")
    if site_id:
        return team.site_set.filter(id=site_id).first()
    return team.site_set.order_by("created_at").first()


def _commissioning_candidates(gateway):
    if not gateway:
        return []

    from .deployment_setup import confidence_explanation, confidence_label

    registered_ports = {
        str(device.port): device.name for device in gateway.devices.exclude(port__isnull=True).exclude(port="")
    }
    latest_run = gateway.deployment_setup_runs.order_by("-created_at").first()
    draft_items = {
        item.discovery_index: item
        for item in (
            latest_run.items.filter(device__isnull=True, discovery_index__isnull=False).select_related(
                "selected_template"
            )
            if latest_run
            else []
        )
    }
    candidates = []
    for index, discovery in enumerate((gateway.discovery_data or {}).get("devices", [])):
        draft_item = draft_items.get(index)
        draft_data = dict(draft_item.candidate_data or {}) if draft_item else {}
        draft_connection = dict(draft_item.connection or {}) if draft_item else {}
        interface = str(discovery.get("interface") or discovery.get("port") or "")
        host = str(draft_connection.get("host") or discovery.get("host") or "")
        port = draft_connection.get("port") or discovery.get("port")
        if not host and (discovery.get("connection") or discovery.get("protocol")) == "modbus_tcp":
            parsed_host, separator, raw_port = interface.rpartition(":")
            host = parsed_host if separator else interface
            port = int(raw_port) if separator and raw_port.isdigit() else 502
        matched_template = draft_item.selected_template if draft_item else None
        matched_template_id = discovery.get("matched_template_id")
        if not matched_template and matched_template_id:
            matched_template = visible_templates_for_team(gateway.team).filter(id=matched_template_id).first()
        score = min(100, max(0, int(discovery.get("matched_template_score") or 0)))
        if interface and interface in registered_ports:
            continue
        status = (
            "ready"
            if matched_template and (draft_item or (matched_template.is_verified and score >= 80))
            else "needs_template"
        )
        draft_saved = bool(draft_item)
        template_requested = bool(draft_data.get("template_request_reference"))
        candidates.append(
            {
                "index": index,
                "interface": interface,
                "signature": draft_data.get("customer_name") or discovery.get("signature") or "Unknown device",
                "connection": discovery.get("connection") or "unknown",
                "host": host,
                "port": port,
                "slave_id": draft_connection.get("slave_id") or discovery.get("slave_id"),
                "protocol_verified": bool(discovery.get("protocol_verified")),
                "baud_rate": discovery.get("baud_rate"),
                "matched_template": matched_template,
                "matched_template_name": discovery.get("matched_template_name")
                or (matched_template.name if matched_template else ""),
                "confidence_score": score,
                "confidence_label": confidence_label(score),
                "confidence_explanation": confidence_explanation(discovery),
                "trust_label": (
                    "Novena verified"
                    if matched_template and matched_template.is_verified
                    else (
                        "AI draft" if matched_template and matched_template.source == "ai_generated" else "Unvalidated"
                    )
                ),
                "status": status,
                "recommended": status == "ready",
                "selected": bool(matched_template and status == "ready"),
                "draft_saved": draft_saved,
                "template_requested": template_requested,
                "template_request_reference": draft_data.get("template_request_reference", ""),
                "template_request_manufacturer": draft_data.get("template_request_manufacturer", ""),
                "template_request_model": draft_data.get("template_request_model", ""),
                "raw": discovery,
            }
        )
    return candidates


def build_commissioning_context(team, gateway=None, session=None):
    """Return a normalized, template-friendly commissioning state for onboarding and gateway pages."""
    gateway = _gateway_from_context(team, gateway=gateway, session=session)
    site = _site_from_context(team, gateway=gateway, session=session)
    devices = list(gateway.devices.select_related("template", "site") if gateway else [])
    candidates = _commissioning_candidates(gateway)
    latest_config = gateway.config_history.first() if gateway else None
    first_live_device = next((device for device in devices if device.last_telemetry_at), None)
    setup_run = None
    readiness = None
    if gateway:
        from .deployment_setup import gateway_readiness, sync_setup_run
        from .models import DeploymentSetupRun

        setup_run = DeploymentSetupRun.objects.filter(team=team, gateway=gateway).order_by("-created_at").first()
        if setup_run:
            setup_run = sync_setup_run(setup_run)
        readiness = gateway_readiness(gateway)

    setup_items = list(
        setup_run.items.select_related("device", "selected_template") if setup_run else []
    )
    candidate_indexes = {candidate["index"] for candidate in candidates}
    equipment_setup_items = [
        item for item in setup_items if item.device_id or item.discovery_index not in candidate_indexes
    ]
    completed_item_states = {"validated", "queued", "applied", "telemetry_confirmed"}
    completed_equipment_count = sum(
        item.state in completed_item_states for item in equipment_setup_items
    )
    review_item_count = sum(
        item.state not in completed_item_states and item.state != "validating"
        for item in equipment_setup_items
    )
    equipment_count = len(candidates) + len(equipment_setup_items)

    completed = []
    if site:
        completed.append("site_created")
    if gateway:
        completed.append("gateway_claimed")
    gateway_state = gateway.freshness if gateway else None
    if gateway_state and gateway_state.status == "live":
        completed.append("gateway_connected")
    if gateway and (gateway.lifecycle_status == "commissioning" or devices or candidates):
        completed.append("device_scan_running")
    if devices or candidates:
        completed.append("devices_discovered")
    if devices or any(candidate["matched_template"] for candidate in candidates):
        completed.append("templates_selected")
    if latest_config and latest_config.status in {"accepted", "active"}:
        completed.append("config_pushed")
    if first_live_device:
        completed.append("first_telemetry_received")
        completed.append("dashboard_ready")

    stage_keys = [stage[0] for stage in COMMISSIONING_STAGES]
    current_stage = next((stage for stage in stage_keys if stage not in completed), "dashboard_ready")
    blocking_stage = current_stage if current_stage != "dashboard_ready" else None

    primary_actions = {
        "site_created": "Create site",
        "gateway_claimed": "Claim gateway",
        "gateway_connected": "Power on gateway",
        "device_scan_running": "Scan for equipment",
        "devices_discovered": "Wait for scan results",
        "templates_selected": "Select templates",
        "config_pushed": "Provision selected equipment",
        "first_telemetry_received": "Wait for first telemetry",
        "dashboard_ready": "Open dashboard",
    }
    messages = {
        "site_created": "Create the physical site where this gateway will be installed.",
        "gateway_claimed": "Claim the gateway using the serial number and sticker claim code.",
        "gateway_connected": "Gateway claimed. Power it on and connect it to the network.",
        "device_scan_running": "Gateway is online. Start or wait for the equipment discovery scan.",
        "devices_discovered": "Review discovered equipment and pick the correct hardware templates.",
        "templates_selected": "Templates are selected. Provision devices to push config to the gateway.",
        "config_pushed": "Config has been sent. Waiting for the gateway to apply it.",
        "first_telemetry_received": "Devices are configured. Waiting for the first live telemetry sample.",
        "dashboard_ready": "Live data is flowing. The dashboard is ready.",
    }

    checklist = [
        {"key": key, "label": label, "complete": key in completed, "current": key == current_stage}
        for key, label in COMMISSIONING_STAGES
    ]
    return {
        "current_stage": current_stage,
        "completed_stages": completed,
        "blocking_stage": blocking_stage,
        "primary_action": {"label": primary_actions[current_stage], "stage": current_stage},
        "status_message": messages[current_stage],
        "checklist": checklist,
        "site": site,
        "gateway": gateway,
        "gateway_state": gateway_state,
        "device_candidates": candidates,
        "ready_candidates": [candidate for candidate in candidates if candidate["status"] == "ready"],
        "needs_template_candidates": [candidate for candidate in candidates if candidate["status"] == "needs_template"],
        "registered_candidates": [candidate for candidate in candidates if candidate["status"] == "registered"],
        "equipment_count": equipment_count,
        "completed_equipment_count": completed_equipment_count,
        "review_equipment_count": len(candidates) + review_item_count,
        "validation_equipment_count": sum(item.state == "validating" for item in equipment_setup_items),
        "selected_candidate_count": sum(candidate["selected"] for candidate in candidates),
        "equipment_setup_items": equipment_setup_items,
        "provisioned_devices": devices,
        "latest_config_status": latest_config.status if latest_config else None,
        "latest_config": latest_config,
        "setup_run": setup_run,
        "readiness": readiness,
        "first_live_device": first_live_device,
        "dashboard_ready": current_stage == "dashboard_ready",
    }
