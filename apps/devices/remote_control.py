from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.teams.models import Team

from .control_governance import GovernanceDenied
from .models import CommandEvent, CommandOutbox, CommandTransportAttempt, Device, RemoteCommand
from .remote_control_protocol import state_change_capability_error


class CommandDenied(ValueError):
    def __init__(self, message: str, *, code: str, command: RemoteCommand | None = None):
        super().__init__(message)
        self.code = code
        self.command = command


@dataclass(frozen=True)
class CommandDefinition:
    operation: str
    risk: str
    state_changing: bool
    permission: str
    device_required: bool = False


COMMAND_CATALOG = {
    "ping": CommandDefinition("ping", RemoteCommand.Risk.DIAGNOSTIC, False, "request_low_risk_commands"),
    "get_status": CommandDefinition("get_status", RemoteCommand.Risk.DIAGNOSTIC, False, "request_low_risk_commands"),
    "get_devices": CommandDefinition("get_devices", RemoteCommand.Risk.DIAGNOSTIC, False, "request_low_risk_commands"),
    "get_config_status": CommandDefinition(
        "get_config_status", RemoteCommand.Risk.DIAGNOSTIC, False, "request_low_risk_commands"
    ),
    "network_preflight": CommandDefinition(
        "network_preflight", RemoteCommand.Risk.DIAGNOSTIC, False, "request_low_risk_commands"
    ),
    "hardware_preflight": CommandDefinition(
        "hardware_preflight", RemoteCommand.Risk.DIAGNOSTIC, False, "request_low_risk_commands"
    ),
    "privilege_preflight": CommandDefinition(
        "privilege_preflight", RemoteCommand.Risk.DIAGNOSTIC, False, "request_low_risk_commands"
    ),
    "scan_devices": CommandDefinition("scan_devices", RemoteCommand.Risk.LOW, False, "request_low_risk_commands"),
    "deployment_preflight": CommandDefinition(
        "deployment_preflight", RemoteCommand.Risk.DIAGNOSTIC, False, "manage_devices"
    ),
    "deployment_discover": CommandDefinition(
        "deployment_discover", RemoteCommand.Risk.DIAGNOSTIC, False, "manage_devices"
    ),
    "deployment_validate": CommandDefinition(
        "deployment_validate", RemoteCommand.Risk.DIAGNOSTIC, False, "manage_devices"
    ),
    "read_device": CommandDefinition(
        "read_device",
        RemoteCommand.Risk.DIAGNOSTIC,
        False,
        "request_low_risk_commands",
        device_required=True,
    ),
    "write_device": CommandDefinition(
        "write_device",
        RemoteCommand.Risk.HIGH,
        True,
        "request_high_risk_commands",
        device_required=True,
    ),
    "restart_connector": CommandDefinition(
        "restart_connector", RemoteCommand.Risk.HIGH, True, "request_high_risk_commands"
    ),
    "restart_all": CommandDefinition("restart_all", RemoteCommand.Risk.HIGH, True, "request_high_risk_commands"),
    "set_log_level": CommandDefinition("set_log_level", RemoteCommand.Risk.HIGH, True, "request_high_risk_commands"),
    "reboot": CommandDefinition("reboot", RemoteCommand.Risk.CRITICAL, True, "request_critical_commands"),
    "update_firmware": CommandDefinition(
        "update_firmware", RemoteCommand.Risk.CRITICAL, True, "request_critical_commands"
    ),
}

STATUS_ORDER = {
    RemoteCommand.Status.REQUESTED: 0,
    RemoteCommand.Status.AWAITING_APPROVAL: 1,
    RemoteCommand.Status.APPROVED: 2,
    RemoteCommand.Status.QUEUED: 3,
    RemoteCommand.Status.DISPATCHING: 4,
    RemoteCommand.Status.PUBLISH_ACCEPTED: 5,
    RemoteCommand.Status.OUTCOME_UNKNOWN: 5,
    RemoteCommand.Status.BROKER_ACKNOWLEDGED: 6,
    RemoteCommand.Status.GATEWAY_RECEIVED: 7,
    RemoteCommand.Status.EXECUTING: 8,
    RemoteCommand.Status.FIELD_PROTOCOL_ACCEPTED: 9,
    RemoteCommand.Status.ACTION_INITIATED: 9,
    RemoteCommand.Status.VERIFICATION_PENDING: 10,
    RemoteCommand.Status.VERIFIED: 11,
    RemoteCommand.Status.ACTION_COMPLETED: 11,
    RemoteCommand.Status.POLICY_DENIED: 100,
    RemoteCommand.Status.REJECTED: 100,
    RemoteCommand.Status.FAILED: 100,
    RemoteCommand.Status.EXPIRED: 100,
    RemoteCommand.Status.TIMED_OUT: 100,
    RemoteCommand.Status.CANCELLED: 100,
    RemoteCommand.Status.RECONCILED_VERIFIED: 110,
    RemoteCommand.Status.RECONCILED_NOT_APPLIED: 110,
    RemoteCommand.Status.RECONCILED_UNRESOLVED: 110,
}

TERMINAL_STATUSES = {
    RemoteCommand.Status.POLICY_DENIED,
    RemoteCommand.Status.REJECTED,
    RemoteCommand.Status.FAILED,
    RemoteCommand.Status.EXPIRED,
    RemoteCommand.Status.TIMED_OUT,
    RemoteCommand.Status.CANCELLED,
    RemoteCommand.Status.VERIFIED,
    RemoteCommand.Status.ACTION_COMPLETED,
    RemoteCommand.Status.RECONCILED_VERIFIED,
    RemoteCommand.Status.RECONCILED_NOT_APPLIED,
    RemoteCommand.Status.RECONCILED_UNRESOLVED,
}


def actor_snapshot(user) -> dict:
    if not user or not getattr(user, "is_authenticated", False):
        return {"type": "system"}
    display = user.get_display_name() if hasattr(user, "get_display_name") else str(user)
    return {
        "type": "user",
        "id": user.pk,
        "email": getattr(user, "email", ""),
        "display_name": display,
    }


def _append_command_event_locked(
    command: RemoteCommand,
    event_type: str,
    *,
    actor=None,
    from_status: str = "",
    to_status: str = "",
    evidence: dict | None = None,
) -> CommandEvent:
    previous = command.events.order_by("-sequence_number").first()
    sequence_number = previous.sequence_number + 1 if previous else 1
    payload = {
        "command": str(command.pk),
        "sequence_number": sequence_number,
        "event_type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "actor": actor_snapshot(actor),
        "evidence": evidence or {},
        "previous": previous.checksum if previous else "",
    }
    checksum = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return CommandEvent.objects.create(
        command=command,
        sequence_number=sequence_number,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_snapshot=payload["actor"],
        evidence=evidence or {},
        checksum=checksum,
    )


def append_command_event(
    command: RemoteCommand,
    event_type: str,
    *,
    actor=None,
    from_status: str = "",
    to_status: str = "",
    evidence: dict | None = None,
) -> CommandEvent:
    """Append hash-chained evidence while serializing on the parent command."""
    with transaction.atomic():
        locked = RemoteCommand.objects.select_for_update().get(pk=command.pk)
        return _append_command_event_locked(
            locked,
            event_type,
            actor=actor,
            from_status=from_status,
            to_status=to_status,
            evidence=evidence,
        )


def transition_command(
    command: RemoteCommand | str,
    to_status: str,
    event_type: str,
    *,
    actor=None,
    evidence: dict | None = None,
    updates: dict | None = None,
) -> tuple[RemoteCommand, bool]:
    """Atomically apply an idempotent, monotonic lifecycle transition and event."""
    command_id = command.pk if isinstance(command, RemoteCommand) else command
    with transaction.atomic():
        locked = RemoteCommand.objects.select_for_update().get(pk=command_id)
        previous = locked.status
        if previous == to_status:
            if updates:
                for field, value in updates.items():
                    setattr(locked, field, value)
                locked.save(update_fields=[*updates.keys(), "updated_at"])
            return locked, False
        if previous in TERMINAL_STATUSES or STATUS_ORDER.get(to_status, -1) < STATUS_ORDER.get(previous, -1):
            return locked, False
        locked.status = to_status
        update_fields = ["status", "updated_at"]
        for field, value in (updates or {}).items():
            setattr(locked, field, value)
            update_fields.append(field)
        locked.save(update_fields=list(dict.fromkeys(update_fields)))
        _append_command_event_locked(
            locked,
            event_type,
            actor=actor,
            from_status=previous,
            to_status=to_status,
            evidence=evidence,
        )
        return locked, True


def _has_permission(user, team, permission: str, *, site=None) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    from apps.teams.roles import has_permission

    return has_permission(user, team, permission, site=site)


def _normalize_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        lowered = stripped.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        with contextlib.suppress(ValueError):
            return float(stripped)
    return value


def resolve_device_params(device: Device, command_key: str, operation: str, value=None) -> tuple[dict, dict]:
    from .datapoint_maps import device_requires_mapping, effective_register_map

    register_map = effective_register_map(device)
    register = register_map.get(command_key)
    if not isinstance(register, dict) or "address" not in register:
        raise CommandDenied(
            f"No mapped register is configured for '{command_key}'.",
            code="unmapped_command_key",
        )

    is_write = operation == "write_device"
    if is_write and device_requires_mapping(device):
        raise CommandDenied(
            "Customer-defined writable signals require Novena governed control commissioning.",
            code="site_defined_control_not_commissioned",
        )
    if is_write and not register.get("writable"):
        raise CommandDenied(f"'{command_key}' is not configured as writable.", code="key_not_writable")
    if is_write and not device.template.is_verified:
        raise CommandDenied(
            "Remote writes require a verified exact-device template.",
            code="template_not_verified",
        )

    normalized = _normalize_value(value)
    if is_write:
        allowed_values = register.get("enum") or register.get("allowed_values")
        if allowed_values is not None and normalized not in allowed_values:
            raise CommandDenied("Value is not in the configured allowed set.", code="value_not_allowed")
        minimum = register.get("min")
        maximum = register.get("max")
        if minimum is not None and (not isinstance(normalized, int | float) or normalized < minimum):
            raise CommandDenied(f"Value must be at least {minimum}.", code="value_below_minimum")
        if maximum is not None and (not isinstance(normalized, int | float) or normalized > maximum):
            raise CommandDenied(f"Value must be at most {maximum}.", code="value_above_maximum")

    params = {
        "device_id": str(device.pk),
        "device_name": device.name,
        "functionCode": int(register.get("functionCode", 6 if is_write else 3)),
        "address": int(register["address"]),
    }
    if register.get("type") and register.get("type") != "bool":
        params["type"] = register["type"]
    if is_write:
        params["value"] = normalized
    else:
        params["objectsCount"] = int(register.get("objectsCount", 1))

    definition_snapshot = {
        "template_id": device.template_id,
        "template_verified": bool(device.template and device.template.is_verified),
        "command_key": command_key,
        "unit": register.get("unit", ""),
        "data_type": register.get("type", ""),
        "minimum": register.get("min"),
        "maximum": register.get("max"),
        "allowed_values": register.get("enum") or register.get("allowed_values"),
    }
    return params, definition_snapshot


@transaction.atomic
def _request_remote_command_atomic(
    *,
    gateway,
    operation: str,
    requested_by,
    params: dict | None = None,
    device: Device | None = None,
    command_key: str = "",
    value=None,
    reason: str = "",
    source: str = RemoteCommand.Source.USER,
    ttl_seconds: int = 30,
) -> RemoteCommand:
    definition = COMMAND_CATALOG.get(operation)
    if not definition:
        raise CommandDenied(f"Unsupported remote operation '{operation}'.", code="unsupported_operation")
    gateway = gateway.__class__.objects.select_for_update().get(pk=gateway.pk)
    if device:
        device = Device.objects.select_for_update(of=("self",)).select_related("template").get(pk=device.pk)
    if definition.device_required and not device:
        raise CommandDenied("This operation requires an exact device target.", code="device_required")
    if operation == "write_device" and not command_key:
        raise CommandDenied("A canonical writable command key is required.", code="command_key_required")
    if device and (device.team_id != gateway.team_id or device.gateway_id != gateway.id):
        raise CommandDenied("The target device does not belong to this Gateway.", code="target_mismatch")

    team = Team.objects.select_for_update().get(pk=gateway.team_id)
    denial: tuple[str, str] | None = None
    if gateway.lifecycle_status in {"release_pending", "released"}:
        denial = (
            "gateway_quarantined",
            "This Gateway is being securely released and cannot accept commands.",
        )
    elif source == RemoteCommand.Source.USER and not _has_permission(
        requested_by,
        team,
        definition.permission,
        site=device.site if device else gateway.site,
    ):
        denial = ("permission_denied", "You do not have permission to request this command.")
    elif operation in {
        "scan_devices",
        "deployment_preflight",
        "deployment_discover",
        "deployment_validate",
    } and not gateway.remote_control_clock_ready:
        denial = (
            "gateway_clock_not_ready",
            "The Gateway clock is still synchronizing. Wait a moment, then retry.",
        )
    elif definition.state_changing and team.remote_control_mode != Team.RemoteControlMode.CONTROLLED:
        denial = ("monitoring_only", "Remote state-changing commands are disabled for this team.")
    elif definition.state_changing and (capability_denial := state_change_capability_error(gateway)):
        denial = capability_denial

    normalized = _normalize_value(value)
    request_params = dict(params or {})
    policy_snapshot = {
        "phase": "governance",
        "team_mode": team.remote_control_mode,
        "permission": definition.permission,
        "state_changing": definition.state_changing,
    }
    template_revision = 0
    commissioning_revision = 0
    policy_revision = 0
    policy_checksum = ""
    if device:
        if operation in {"read_device", "write_device"}:
            try:
                request_params, register_snapshot = resolve_device_params(device, command_key, operation, value)
                policy_snapshot["register"] = register_snapshot
                normalized = request_params.get("value")
                if operation == "write_device" and team.remote_control_mode == Team.RemoteControlMode.CONTROLLED:
                    from .control_governance import (
                        effective_control_envelope,
                        validate_control_value,
                    )

                    envelope = effective_control_envelope(
                        device=device,
                        command_key=command_key,
                        user=requested_by,
                    )
                    normalized, encoded = validate_control_value(
                        envelope=envelope,
                        value=value,
                        device=device,
                    )
                    mapping = envelope.template.connector_mapping
                    request_params = {
                        "device_id": str(device.pk),
                        "device_name": device.name,
                        "command_key": command_key,
                        "functionCode": int(mapping["functionCode"]),
                        "address": int(mapping["address"]),
                        "value": encoded,
                        "expected_value": normalized,
                        "unit": envelope.template.unit,
                        "data_type": envelope.template.data_type,
                        "prerequisites": envelope.prerequisites,
                    }
                    if mapping.get("type"):
                        request_params["type"] = mapping["type"]
                    policy_snapshot["effective_envelope"] = envelope.snapshot()
                    template_revision = envelope.template.revision
                    commissioning_revision = envelope.commissioned.revision
                    policy_revision = envelope.policy.revision
                    policy_checksum = envelope.policy.checksum
            except CommandDenied as exc:
                denial = denial or (exc.code, str(exc))
            except GovernanceDenied as exc:
                denial = denial or (exc.code, str(exc))
        request_params["device_id"] = str(device.pk)
        request_params["device_name"] = device.name

    previous = RemoteCommand.objects.filter(device=device).order_by("-sequence_number").first() if device else None
    sequence_number = (previous.sequence_number + 1) if previous else 1
    approval_required = bool(policy_snapshot.get("effective_envelope", {}).get("approval_required"))
    status = (
        RemoteCommand.Status.POLICY_DENIED
        if denial
        else (RemoteCommand.Status.AWAITING_APPROVAL if approval_required else RemoteCommand.Status.QUEUED)
    )
    command = RemoteCommand.objects.create(
        team=team,
        gateway=gateway,
        device=device,
        requested_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
        source=source,
        operation=operation,
        command_key=command_key,
        requested_value=value,
        normalized_value=normalized,
        reason=reason,
        risk=definition.risk,
        status=status,
        actor_snapshot=actor_snapshot(requested_by),
        target_snapshot={
            "gateway_serial": gateway.serial_number,
            "gateway_name": gateway.name,
            **({"device_id": device.pk, "device_name": device.name} if device else {}),
        },
        policy_snapshot=policy_snapshot,
        request_payload={"method": operation, "params": request_params},
        template_revision=template_revision,
        commissioning_revision=commissioning_revision,
        policy_revision=policy_revision,
        policy_checksum=policy_checksum,
        control_epoch=team.remote_control_epoch,
        sequence_number=sequence_number,
        expires_at=timezone.now() + timedelta(seconds=max(5, min(ttl_seconds, 300))),
        error_code=denial[0] if denial else "",
        error_message=denial[1] if denial else "",
    )
    append_command_event(
        command,
        "policy_denied" if denial else ("approval_requested" if approval_required else "command_queued"),
        actor=requested_by,
        from_status=RemoteCommand.Status.REQUESTED,
        to_status=status,
        evidence={"code": denial[0]} if denial else {"operation": operation},
    )
    if denial:
        return command, denial
    if approval_required:
        from .control_readiness import create_approval

        create_approval(command)
        return command, None

    outbox = CommandOutbox.objects.create(command=command)
    transaction.on_commit(lambda: _schedule_outbox_dispatch(outbox.pk))
    return command, None


def request_remote_command(*args, **kwargs) -> RemoteCommand:
    """Persist every policy decision, then surface denials after the audit transaction commits."""
    command, denial = _request_remote_command_atomic(*args, **kwargs)
    if denial:
        raise CommandDenied(denial[1], code=denial[0], command=command)
    return command


def _schedule_outbox_dispatch(outbox_id: int) -> None:
    from .tasks import dispatch_remote_command_outbox

    dispatch_remote_command_outbox.delay(outbox_id)


def _dispatch_revalidation_error(command, team, gateway, device) -> tuple[str, str] | None:
    definition = COMMAND_CATALOG.get(command.operation)
    if not definition:
        return ("unsupported_operation", "The command operation is no longer supported.")
    if command.expires_at <= timezone.now():
        return ("command_expired", "The command expired before dispatch.")
    if command.gateway_id != gateway.pk or command.team_id != team.pk:
        return ("gateway_identity_changed", "The command Gateway identity is no longer valid.")
    if definition.state_changing:
        if team.remote_control_mode != Team.RemoteControlMode.CONTROLLED:
            return ("remote_control_revoked", "Remote control was disabled before dispatch.")
        if command.control_epoch != team.remote_control_epoch:
            return ("control_epoch_changed", "The control epoch changed before dispatch.")
        if capability_denial := state_change_capability_error(gateway):
            return capability_denial
    if command.operation != "write_device":
        return None
    if not device or device.gateway_id != gateway.pk or device.team_id != team.pk:
        return ("device_identity_changed", "The canonical device no longer belongs to this Gateway.")
    params = command.request_payload.get("params", {})
    if params.get("device_id") != str(device.pk) or not command.command_key:
        return ("canonical_target_invalid", "The command does not contain one canonical device and writable key.")
    if (
        not gateway.remote_control_policy_loaded
        or gateway.remote_control_epoch != command.control_epoch
        or gateway.remote_control_policy_revision != command.policy_revision
        or not gateway.remote_control_clock_ready
        or not gateway.remote_control_journal_ready
        or not gateway.remote_control_storage_healthy
    ):
        return ("gateway_readiness_stale", "The Gateway has not advertised matching governed-control readiness.")

    from .models import (
        CommandPolicy,
        CommissionedControlEnvelope,
        ControlActivation,
        RemoteControlScope,
        TemplateControlDefinition,
    )

    scope = (
        RemoteControlScope.objects.select_for_update()
        .filter(
            team=team,
            gateway=gateway,
            device=device,
            command_key=command.command_key,
            mode=RemoteControlScope.Mode.ENABLED,
            control_epoch=command.control_epoch,
        )
        .first()
    )
    definition_row = (
        TemplateControlDefinition.objects.select_for_update()
        .filter(
            template_id=device.template_id,
            command_key=command.command_key,
            revision=command.template_revision,
            is_verified=True,
            is_enabled=True,
        )
        .first()
    )
    commissioned = (
        CommissionedControlEnvelope.objects.select_for_update()
        .filter(
            team=team,
            device=device,
            command_key=command.command_key,
            revision=command.commissioning_revision,
            is_active=True,
        )
        .first()
    )
    policy = (
        CommandPolicy.objects.select_for_update()
        .filter(
            team=team,
            gateway=gateway,
            device=device,
            command_key=command.command_key,
            revision=command.policy_revision,
            checksum=command.policy_checksum,
            is_enabled=True,
        )
        .first()
    )
    activation = (
        ControlActivation.objects.select_for_update()
        .filter(
            team=team,
            device=device,
            command_key=command.command_key,
            status=ControlActivation.Status.ACTIVE,
            control_epoch=command.control_epoch,
        )
        .first()
    )
    if not all([scope, definition_row, commissioned, policy, activation]):
        return ("governance_changed", "The command's governing records changed before dispatch.")
    if commissioned.expires_at and commissioned.expires_at <= timezone.now():
        return ("commissioning_expired", "The commissioned control envelope expired before dispatch.")
    if activation.expires_at <= timezone.now():
        return ("activation_expired", "The control activation expired before dispatch.")
    return None


def _cancel_claim_locked(outbox, command, *, code, message):
    previous = command.status
    target = RemoteCommand.Status.EXPIRED if code == "command_expired" else RemoteCommand.Status.CANCELLED
    outbox.status = CommandOutbox.Status.CANCELLED
    outbox.last_error = message
    outbox.lease_expires_at = None
    outbox.save(update_fields=["status", "last_error", "lease_expires_at", "updated_at"])
    command.status = target
    command.transport_status = "cancelled"
    command.error_code = code
    command.error_message = message
    command.save(update_fields=["status", "transport_status", "error_code", "error_message", "updated_at"])
    _append_command_event_locked(
        command,
        "dispatch_cancelled",
        from_status=previous,
        to_status=target,
        evidence={"code": code, "message": message},
    )


def _retry_delay(attempt_count: int) -> timedelta:
    base = int(getattr(settings, "REMOTE_CONTROL_OUTBOX_RETRY_BASE_SECONDS", 5))
    maximum = int(getattr(settings, "REMOTE_CONTROL_OUTBOX_RETRY_MAX_SECONDS", 300))
    return timedelta(seconds=min(maximum, base * (2 ** max(0, attempt_count - 1))))


def _record_pre_ack_failure(outbox_id, attempt_id, exc):
    now = timezone.now()
    max_attempts = int(getattr(settings, "REMOTE_CONTROL_OUTBOX_MAX_ATTEMPTS", 5))
    with transaction.atomic():
        outbox = CommandOutbox.objects.select_for_update().get(pk=outbox_id)
        command = RemoteCommand.objects.select_for_update().get(pk=outbox.command_id)
        attempt = CommandTransportAttempt.objects.select_for_update().get(pk=attempt_id)
        attempt.state = "failed_before_ack"
        attempt.error = str(exc)
        attempt.completed_at = now
        attempt.save(update_fields=["state", "error", "completed_at"])
        outbox.last_error = str(exc)
        outbox.claimed_at = None
        outbox.lease_expires_at = None
        if outbox.attempt_count >= max_attempts:
            previous = command.status
            outbox.status = CommandOutbox.Status.DEAD_LETTER
            outbox.dead_lettered_at = now
            if STATUS_ORDER.get(command.status, -1) <= STATUS_ORDER[RemoteCommand.Status.DISPATCHING]:
                command.status = RemoteCommand.Status.FAILED
            command.transport_status = "dead_letter"
            command.error_code = "publish_attempts_exhausted"
            command.error_message = str(exc)
            outbox.save(
                update_fields=[
                    "status",
                    "last_error",
                    "claimed_at",
                    "lease_expires_at",
                    "dead_lettered_at",
                    "updated_at",
                ]
            )
            command.save(
                update_fields=[
                    "status",
                    "transport_status",
                    "error_code",
                    "error_message",
                    "updated_at",
                ]
            )
            _append_command_event_locked(
                command,
                "outbox_dead_lettered",
                from_status=previous,
                to_status=command.status,
                evidence={"attempt": outbox.attempt_count, "error": str(exc)},
            )
        else:
            outbox.status = CommandOutbox.Status.RETRY
            outbox.next_attempt_at = now + _retry_delay(outbox.attempt_count)
            command.transport_status = "retry_scheduled"
            command.error_code = "publish_failed_before_ack"
            command.error_message = str(exc)
            outbox.save(
                update_fields=[
                    "status",
                    "next_attempt_at",
                    "last_error",
                    "claimed_at",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            command.save(update_fields=["transport_status", "error_code", "error_message", "updated_at"])
            _append_command_event_locked(
                command,
                "outbox_retry_scheduled",
                from_status=command.status,
                to_status=command.status,
                evidence={
                    "attempt": outbox.attempt_count,
                    "next_attempt_at": outbox.next_attempt_at.isoformat(),
                    "error": str(exc),
                },
            )
        return command


def dispatch_outbox(outbox_id: int) -> RemoteCommand | None:
    from apps.telemetry.mqtt_publisher import MqttPublishOutcomeUnknown, publish_rpc_command

    from .remote_control_crypto import build_signed_command_envelope

    now = timezone.now()
    lease_seconds = int(getattr(settings, "REMOTE_CONTROL_OUTBOX_LEASE_SECONDS", 60))
    with transaction.atomic():
        outbox = CommandOutbox.objects.select_for_update(skip_locked=True).filter(pk=outbox_id).first()
        if not outbox:
            return None
        claimable = outbox.status in {CommandOutbox.Status.PENDING, CommandOutbox.Status.RETRY}
        claimable = claimable or (
            outbox.status == CommandOutbox.Status.CLAIMED and outbox.lease_expires_at and outbox.lease_expires_at <= now
        )
        if not claimable or outbox.next_attempt_at > now:
            return None
        command = RemoteCommand.objects.select_for_update().get(pk=outbox.command_id)
        max_attempts = int(getattr(settings, "REMOTE_CONTROL_OUTBOX_MAX_ATTEMPTS", 5))
        if outbox.attempt_count >= max_attempts:
            previous = command.status
            outbox.status = CommandOutbox.Status.DEAD_LETTER
            outbox.dead_lettered_at = now
            outbox.lease_expires_at = None
            outbox.last_error = outbox.last_error or "Expired worker leases exhausted dispatch attempts."
            if STATUS_ORDER.get(command.status, -1) <= STATUS_ORDER[RemoteCommand.Status.DISPATCHING]:
                command.status = RemoteCommand.Status.FAILED
            command.transport_status = "dead_letter"
            command.error_code = "dispatch_attempts_exhausted"
            command.error_message = outbox.last_error
            outbox.save(
                update_fields=[
                    "status",
                    "dead_lettered_at",
                    "lease_expires_at",
                    "last_error",
                    "updated_at",
                ]
            )
            command.save(
                update_fields=[
                    "status",
                    "transport_status",
                    "error_code",
                    "error_message",
                    "updated_at",
                ]
            )
            _append_command_event_locked(
                command,
                "outbox_dead_lettered",
                from_status=previous,
                to_status=command.status,
                evidence={"attempt": outbox.attempt_count, "reason": "expired_worker_lease"},
            )
            return command
        team = Team.objects.select_for_update().get(pk=command.team_id)
        gateway = command.gateway.__class__.objects.select_for_update().get(pk=command.gateway_id)
        device = (
            Device.objects.select_for_update(of=("self",))
            .select_related("template")
            .filter(pk=command.device_id)
            .first()
            if command.device_id
            else None
        )
        if denial := _dispatch_revalidation_error(command, team, gateway, device):
            _cancel_claim_locked(outbox, command, code=denial[0], message=denial[1])
            return command
        outbox.status = CommandOutbox.Status.CLAIMED
        outbox.claimed_at = now
        outbox.lease_expires_at = now + timedelta(seconds=lease_seconds)
        outbox.attempt_count += 1
        outbox.save(update_fields=["status", "claimed_at", "lease_expires_at", "attempt_count", "updated_at"])
        if STATUS_ORDER.get(command.status, -1) < STATUS_ORDER[RemoteCommand.Status.DISPATCHING]:
            previous = command.status
            command.status = RemoteCommand.Status.DISPATCHING
            _append_command_event_locked(
                command,
                "dispatch_claimed",
                from_status=previous,
                to_status=command.status,
                evidence={"attempt": outbox.attempt_count},
            )
        command.transport_status = "dispatching"
        command.save(update_fields=["status", "transport_status", "updated_at"])
        attempt = CommandTransportAttempt.objects.create(
            command=command,
            outbox=outbox,
            attempt_number=outbox.attempt_count,
        )

    try:
        request_id = uuid.uuid4()
        envelope = build_signed_command_envelope(command, request_id=request_id)
        RemoteCommand.objects.filter(pk=command.pk).update(
            signing_key_id=envelope["signing_key_id"],
            signature=envelope["signature"],
        )
        rpc = publish_rpc_command(
            command.gateway,
            command.operation,
            command.request_payload.get("params", {}),
            remote_command=command,
            request_id=request_id,
            governed_envelope=envelope,
        )
    except MqttPublishOutcomeUnknown as exc:
        with transaction.atomic():
            outbox = CommandOutbox.objects.select_for_update().get(pk=outbox_id)
            command = RemoteCommand.objects.select_for_update().get(pk=outbox.command_id)
            attempt = CommandTransportAttempt.objects.select_for_update().get(pk=attempt.pk)
            attempt.request_id = exc.rpc_record.request_id
            attempt.state = "outcome_unknown"
            attempt.error = str(exc)
            attempt.completed_at = timezone.now()
            attempt.save(update_fields=["request_id", "state", "error", "completed_at"])
            outbox.status = CommandOutbox.Status.PUBLISHED
            outbox.last_error = str(exc)
            outbox.lease_expires_at = None
            previous = command.status
            if STATUS_ORDER.get(command.status, -1) <= STATUS_ORDER[RemoteCommand.Status.OUTCOME_UNKNOWN]:
                command.status = RemoteCommand.Status.OUTCOME_UNKNOWN
            command.transport_status = "outcome_unknown"
            command.error_code = "broker_ack_unknown"
            command.error_message = str(exc)
            outbox.save(update_fields=["status", "last_error", "lease_expires_at", "updated_at"])
            command.save(
                update_fields=[
                    "status",
                    "transport_status",
                    "error_code",
                    "error_message",
                    "updated_at",
                ]
            )
            _append_command_event_locked(
                command,
                "broker_ack_unknown",
                from_status=previous,
                to_status=command.status,
                evidence={"request_id": str(exc.rpc_record.request_id)},
            )
        return command
    except Exception as exc:
        return _record_pre_ack_failure(outbox_id, attempt.pk, exc)

    with transaction.atomic():
        outbox = CommandOutbox.objects.select_for_update().get(pk=outbox_id)
        command = RemoteCommand.objects.select_for_update().get(pk=outbox.command_id)
        outbox.status = CommandOutbox.Status.PUBLISHED
        outbox.published_at = timezone.now()
        outbox.lease_expires_at = None
        previous = command.status
        if STATUS_ORDER.get(command.status, -1) < STATUS_ORDER[RemoteCommand.Status.BROKER_ACKNOWLEDGED]:
            command.status = RemoteCommand.Status.BROKER_ACKNOWLEDGED
        command.transport_status = "broker_acknowledged"
        command.broker_acknowledged_at = timezone.now()
        attempt = CommandTransportAttempt.objects.select_for_update().get(pk=attempt.pk)
        attempt.request_id = rpc.request_id
        attempt.state = "broker_acknowledged"
        attempt.completed_at = timezone.now()
        attempt.save(update_fields=["request_id", "state", "completed_at"])
        outbox.save(update_fields=["status", "published_at", "lease_expires_at", "updated_at"])
        command.save(update_fields=["status", "transport_status", "broker_acknowledged_at", "updated_at"])
        _append_command_event_locked(
            command,
            "broker_acknowledged",
            from_status=previous,
            to_status=command.status,
            evidence={"rpc_request_id": str(rpc.request_id)},
        )
    return command


def dispatch_due_outboxes(*, limit=100) -> int:
    """Recover and dispatch committed, retriable, or expired-lease outbox rows."""
    now = timezone.now()
    due_ids = list(
        CommandOutbox.objects.filter(next_attempt_at__lte=now)
        .filter(
            Q(status__in=[CommandOutbox.Status.PENDING, CommandOutbox.Status.RETRY])
            | Q(status=CommandOutbox.Status.CLAIMED, lease_expires_at__lte=now)
        )
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    dispatched = 0
    for outbox_id in due_ids:
        if dispatch_outbox(outbox_id):
            dispatched += 1
    return dispatched
