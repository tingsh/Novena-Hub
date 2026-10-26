"""Canonical Hub ↔ Gateway governed remote-control protocol contract."""

from __future__ import annotations

REMOTE_CONTROL_PROTOCOL_VERSION = 1

CAPABILITY_GOVERNED_COMMANDS = "governed_commands_v1"
CAPABILITY_LOCAL_WRITEBACK = "local_writeback_v1"
CAPABILITY_LIFECYCLE_STAGES = "lifecycle_stages_v1"
CAPABILITY_IDEMPOTENT_REPLAY = "idempotent_replay_v1"

REQUIRED_STATE_CHANGE_CAPABILITIES = frozenset(
    {
        CAPABILITY_GOVERNED_COMMANDS,
        CAPABILITY_LOCAL_WRITEBACK,
        CAPABILITY_LIFECYCLE_STAGES,
        CAPABILITY_IDEMPOTENT_REPLAY,
    }
)

LEGACY_DEVICE_METHOD_MAP = {
    "read": "read_device",
    "read_device": "read_device",
    "write": "write_device",
    "write_device": "write_device",
}

GATEWAY_STAGE_TO_COMMAND_STATUS = {
    "gateway_received": "gateway_received",
    "diagnostic_completed": "verified",
    "executing": "executing",
    "field_protocol_accepted": "field_protocol_accepted",
    "field_execution_verified": "verified",
    "ota_initiated": "action_initiated",
    "gateway_action_completed": "action_completed",
    "rejected": "rejected",
    "failed": "failed",
}


def canonical_device_operation(method: str) -> str:
    """Resolve only explicitly supported compatibility method names."""
    try:
        return LEGACY_DEVICE_METHOD_MAP[method]
    except (KeyError, TypeError) as exc:
        raise ValueError("Unknown or unsupported device command method.") from exc


def advertised_capabilities(gateway) -> set[str]:
    value = getattr(gateway, "remote_control_capabilities", None)
    return {item for item in (value or []) if isinstance(item, str)}


def state_change_capability_error(gateway) -> tuple[str, str] | None:
    """Return a fail-closed reason when a Gateway is command-ineligible."""
    if getattr(gateway, "remote_control_protocol_version", 0) != REMOTE_CONTROL_PROTOCOL_VERSION:
        return (
            "protocol_not_advertised",
            "The Gateway has not advertised governed remote-control protocol version 1.",
        )
    missing = sorted(REQUIRED_STATE_CHANGE_CAPABILITIES - advertised_capabilities(gateway))
    if missing:
        return (
            "capability_not_advertised",
            f"The Gateway is missing required remote-control capabilities: {', '.join(missing)}.",
        )
    if not getattr(gateway, "remote_control_local_writeback_enabled", False):
        return ("writeback_not_advertised", "The Gateway has not explicitly enabled local write-back.")
    return None
