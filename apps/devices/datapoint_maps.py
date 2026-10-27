"""Canonical per-device datapoint maps for programmable equipment."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import DeviceDatapointMap, DeviceDatapointMapRevision

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
DATA_TYPES = {"bool", "uint16", "int16", "uint32", "int32", "float32", "uint64", "int64", "float64"}
DISPLAY_TYPES = {"value", "gauge", "trend", "status"}
ACCESS_TYPES = {"read_only", "read_write"}
READ_FUNCTION_CODES = {1, 2, 3, 4}
WRITE_FUNCTION_CODES = {5, 6, 15, 16}


def default_register_count(data_type) -> int:
    """Return the number of 16-bit Modbus objects used by a signal."""
    normalized = str(data_type or "").lower()
    return 4 if "64" in normalized else 2 if "32" in normalized else 1


CSV_COLUMNS = (
    "key",
    "label",
    "address",
    "function_code",
    "data_type",
    "register_count",
    "unit",
    "scale",
    "offset",
    "access",
    "write_function_code",
    "display_type",
    "normal_min",
    "normal_max",
    "safety_min",
    "safety_max",
    "alert_suggestion",
    "invalid_values",
    "energy_direction",
)


def device_requires_mapping(device) -> bool:
    return bool(device.template and device.template.mapping_strategy == "site_defined")


def _integer(value, *, field, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a whole number."}) from exc
    if not minimum <= parsed <= maximum:
        raise ValidationError({field: f"Enter a value from {minimum} to {maximum}."})
    return parsed


def _number(value, *, field, default=None):
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid number."}) from exc
    if not math.isfinite(parsed):
        raise ValidationError({field: "Enter a finite number."})
    return parsed


def normalize_datapoints(datapoints) -> list[dict]:
    """Validate and normalize user-entered Modbus datapoints."""
    if not isinstance(datapoints, list) or not datapoints:
        raise ValidationError("Add at least one signal.")
    if len(datapoints) > 20:
        raise ValidationError("A device can contain at most 20 signals in Guided Setup.")

    normalized = []
    seen = set()
    for position, raw in enumerate(datapoints, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"Signal {position} is invalid.")
        key = re.sub(r"[^a-z0-9_]+", "_", str(raw.get("key") or "").strip().lower()).strip("_")
        if not KEY_RE.fullmatch(key):
            raise ValidationError(f"Signal {position}: use a key such as process_temperature.")
        if key in seen:
            raise ValidationError(f"Signal key '{key}' is duplicated.")
        seen.add(key)
        if raw.get("address") in (None, ""):
            raise ValidationError(f"Signal '{key}' needs a register address from the equipment source.")

        data_type = str(raw.get("data_type") or raw.get("type") or "uint16").lower()
        if data_type not in DATA_TYPES:
            raise ValidationError(f"Signal '{key}' has an unsupported data type.")
        read_function_code = _integer(
            raw.get("read_function_code", raw.get("functionCode", 3)),
            field="read_function_code",
            minimum=1,
            maximum=4,
        )
        if read_function_code not in READ_FUNCTION_CODES:
            raise ValidationError(f"Signal '{key}' must use a read-only Modbus function code.")
        default_count = default_register_count(data_type)
        raw_objects_count = raw.get("objects_count", raw.get("objectsCount"))
        objects_count = _integer(
            default_count if raw_objects_count in (None, "") else raw_objects_count,
            field="objects_count",
            minimum=1,
            maximum=4,
        )
        if objects_count != default_count:
            raise ValidationError(f"Signal '{key}' needs exactly {default_count} Modbus object(s) for {data_type}.")
        access = str(raw.get("access") or ("read_write" if raw.get("writable") else "read_only"))
        if access not in ACCESS_TYPES:
            raise ValidationError(f"Signal '{key}' has an invalid access setting.")
        write_function_code = None
        if access == "read_write":
            default_write = 5 if data_type == "bool" else (16 if objects_count > 1 else 6)
            write_function_code = _integer(
                raw.get("write_function_code") or raw.get("writeFunctionCode") or default_write,
                field="write_function_code",
                minimum=5,
                maximum=16,
            )
            if write_function_code not in WRITE_FUNCTION_CODES:
                raise ValidationError(f"Signal '{key}' has an invalid write function code.")

        default_display = "status" if data_type == "bool" else "value"
        display_type = str(raw.get("display_type") or default_display)
        if display_type not in DISPLAY_TYPES:
            raise ValidationError(f"Signal '{key}' has an invalid display type.")
        multiplier = _number(raw.get("multiplier", raw.get("scale", 1)), field="multiplier", default=1.0)
        if multiplier == 0:
            raise ValidationError(f"Signal '{key}' multiplier cannot be zero.")
        offset = _number(raw.get("offset", 0), field="offset", default=0.0)
        raw_invalid = raw.get("invalid_values", raw.get("invalidValues", [])) or []
        if isinstance(raw_invalid, str):
            raw_invalid = [value.strip() for value in raw_invalid.split(",") if value.strip()]
        if not isinstance(raw_invalid, list) or len(raw_invalid) > 8:
            raise ValidationError(f"Signal '{key}' has too many invalid-value codes.")
        try:
            invalid_values = [
                int(value) if str(value).lstrip("+-").isdigit() else float(value) for value in raw_invalid
            ]
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Signal '{key}' has an invalid-value code that is not a number.") from exc
        if any(not math.isfinite(value) for value in invalid_values):
            raise ValidationError(f"Signal '{key}' has a nonfinite invalid-value code.")
        energy_direction = str(raw.get("energy_direction") or "none").lower()
        if energy_direction not in {"none", "import", "export", "net"}:
            raise ValidationError(f"Signal '{key}' has an unsupported energy direction.")
        normal_min = _number(raw.get("normal_min"), field="normal_min")
        normal_max = _number(raw.get("normal_max"), field="normal_max")
        safety_min = _number(raw.get("safety_min", raw.get("min")), field="safety_min")
        safety_max = _number(raw.get("safety_max", raw.get("max")), field="safety_max")
        if normal_min is not None and normal_max is not None and normal_min > normal_max:
            raise ValidationError(f"Signal '{key}' normal minimum cannot exceed its maximum.")
        if safety_min is not None and safety_max is not None and safety_min > safety_max:
            raise ValidationError(f"Signal '{key}' safety minimum cannot exceed its maximum.")

        normalized.append(
            {
                "key": key,
                "label": str(raw.get("label") or key.replace("_", " ").title()).strip()[:200],
                "address": _integer(raw.get("address", 0), field="address", minimum=0, maximum=65535),
                "read_function_code": read_function_code,
                "data_type": data_type,
                "objects_count": objects_count,
                "unit": str(raw.get("unit") or "").strip()[:32],
                "multiplier": multiplier,
                "offset": offset,
                "invalid_values": invalid_values,
                "energy_direction": energy_direction,
                "access": access,
                "write_function_code": write_function_code,
                "display_type": display_type,
                "normal_min": normal_min,
                "normal_max": normal_max,
                "safety_min": safety_min,
                "safety_max": safety_max,
                "alert_suggestion": str(raw.get("alert_suggestion") or "").strip()[:300],
            }
        )
    return normalized


def datapoints_to_csv(datapoints) -> str:
    """Serialize a normalized map using spreadsheet-friendly column names."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for point in datapoints or []:
        writer.writerow(
            {
                "key": point.get("key", ""),
                "label": point.get("label", ""),
                "address": point.get("address", ""),
                "function_code": point.get("read_function_code", ""),
                "data_type": point.get("data_type", ""),
                "register_count": point.get("objects_count", ""),
                "unit": point.get("unit", ""),
                "scale": point.get("multiplier", ""),
                "offset": point.get("offset", ""),
                "access": point.get("access", ""),
                "write_function_code": point.get("write_function_code") or "",
                "display_type": point.get("display_type", ""),
                "normal_min": point.get("normal_min") if point.get("normal_min") is not None else "",
                "normal_max": point.get("normal_max") if point.get("normal_max") is not None else "",
                "safety_min": point.get("safety_min") if point.get("safety_min") is not None else "",
                "safety_max": point.get("safety_max") if point.get("safety_max") is not None else "",
                "alert_suggestion": point.get("alert_suggestion", ""),
                "invalid_values": ",".join(str(value) for value in point.get("invalid_values", [])),
                "energy_direction": point.get("energy_direction", "none"),
            }
        )
    return output.getvalue()


def datapoints_from_csv(content: str) -> list[dict]:
    """Parse a complete map and report all discoverable errors with CSV row numbers."""
    try:
        reader = csv.DictReader(io.StringIO(content, newline=""))
        headers = [str(header or "").strip().lower() for header in (reader.fieldnames or [])]
    except csv.Error as exc:
        raise ValidationError(f"The CSV file could not be read: {exc}") from exc

    if not headers:
        raise ValidationError("The CSV file must include a header row.")
    if len(headers) != len(set(headers)):
        raise ValidationError("The CSV header contains duplicate columns.")
    missing = [column for column in ("key", "address") if column not in headers]
    unknown = [column for column in headers if column not in CSV_COLUMNS]
    errors = []
    if missing:
        errors.append(f"Missing required column(s): {', '.join(missing)}.")
    if unknown:
        errors.append(f"Unknown column(s): {', '.join(unknown)}.")
    if errors:
        raise ValidationError(errors)

    normalized = []
    seen = {}
    try:
        for row_number, row in enumerate(reader, start=2):
            row = {str(key or "").strip().lower(): value for key, value in row.items()}
            if not any(str(value or "").strip() for value in row.values()):
                continue
            raw = {
                "key": row.get("key"),
                "label": row.get("label"),
                "address": row.get("address"),
                "read_function_code": row.get("function_code") or 3,
                "data_type": row.get("data_type") or "uint16",
                "objects_count": row.get("register_count") or None,
                "unit": row.get("unit"),
                "multiplier": row.get("scale") or 1,
                "offset": row.get("offset") or 0,
                "access": row.get("access") or "read_only",
                "write_function_code": row.get("write_function_code") or None,
                "display_type": row.get("display_type") or None,
                "normal_min": row.get("normal_min"),
                "normal_max": row.get("normal_max"),
                "safety_min": row.get("safety_min"),
                "safety_max": row.get("safety_max"),
                "alert_suggestion": row.get("alert_suggestion"),
                "invalid_values": row.get("invalid_values"),
                "energy_direction": row.get("energy_direction"),
            }
            try:
                point = normalize_datapoints([raw])[0]
            except ValidationError as exc:
                errors.extend(f"Row {row_number}: {message}" for message in exc.messages)
                continue
            if point["key"] in seen:
                errors.append(f"Row {row_number}: signal key '{point['key']}' duplicates row {seen[point['key']]}.")
                continue
            seen[point["key"]] = row_number
            normalized.append(point)
    except csv.Error as exc:
        errors.append(f"The CSV file could not be read: {exc}")

    if len(normalized) > 20:
        errors.append("The CSV contains more than 20 signals.")
    if not normalized and not errors:
        errors.append("The CSV file must contain at least one signal row.")
    if errors:
        raise ValidationError(errors)
    return normalized


def register_map_to_datapoints(register_map) -> list[dict]:
    rows = []
    for key, config in (register_map or {}).items():
        if not isinstance(config, dict):
            continue
        legacy_function_code = config.get("functionCode", 3)
        read_function_code = config.get("readFunctionCode")
        if read_function_code is None:
            read_function_code = (
                legacy_function_code
                if legacy_function_code in READ_FUNCTION_CODES
                else (1 if config.get("type") == "bool" else 3)
            )
        write_function_code = config.get("writeFunctionCode")
        if write_function_code is None and legacy_function_code in WRITE_FUNCTION_CODES:
            write_function_code = legacy_function_code
        rows.append(
            {
                "key": key,
                "label": config.get("label") or key.replace("_", " ").title(),
                "address": config.get("address", 0),
                "read_function_code": read_function_code,
                "data_type": config.get("type", "uint16"),
                "objects_count": config.get("objectsCount"),
                "unit": config.get("unit", ""),
                "multiplier": config.get("multiplier", config.get("scale", 1)),
                "offset": config.get("offset", 0),
                "access": "read_write" if config.get("writable") else "read_only",
                "write_function_code": write_function_code,
                "display_type": config.get("display_type", "status" if config.get("type") == "bool" else "value"),
                "normal_min": config.get("normal_min"),
                "normal_max": config.get("normal_max"),
                "safety_min": config.get("safety_min", config.get("min")),
                "safety_max": config.get("safety_max", config.get("max")),
                "alert_suggestion": config.get("alert_suggestion", ""),
                "invalid_values": config.get("invalidValues", []),
                "energy_direction": config.get("energy_direction", "none"),
            }
        )
    return normalize_datapoints(rows) if rows else []


def datapoints_to_register_map(datapoints) -> dict:
    result = {}
    for point in datapoints or []:
        config = {
            "label": point["label"],
            "address": point["address"],
            "functionCode": point["read_function_code"],
            "readFunctionCode": point["read_function_code"],
            "type": point["data_type"],
            "objectsCount": point["objects_count"],
            "unit": point["unit"],
            "multiplier": point["multiplier"],
            "offset": point["offset"],
            "writable": point["access"] == "read_write",
            "poll": True,
            "display_type": point["display_type"],
            "normal_min": point.get("normal_min"),
            "normal_max": point.get("normal_max"),
            "safety_min": point.get("safety_min"),
            "safety_max": point.get("safety_max"),
            "alert_suggestion": point.get("alert_suggestion", ""),
            "energy_direction": point.get("energy_direction", "none"),
        }
        if point.get("invalid_values"):
            config["invalidValues"] = point["invalid_values"]
        if point.get("write_function_code"):
            config["writeFunctionCode"] = point["write_function_code"]
        result[point["key"]] = config
    return result


def mapping_checksum(device, datapoints=None) -> str:
    if datapoints is None:
        mapping = getattr(device, "datapoint_map", None)
        datapoints = mapping.datapoints if mapping else []
    payload = {
        "protocol": device.protocol,
        "connection": device.connection_config or {},
        "datapoints": datapoints or [],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def datapoints_checksum(datapoints) -> str:
    canonical = json.dumps(datapoints or [], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def effective_register_map(device, *, require_confirmed=True) -> dict:
    """Return deployable/display metadata without confusing a PLC starter with its site map."""
    if not device_requires_mapping(device):
        if device.template and isinstance(device.template.register_map, dict):
            return device.template.register_map
        return {}
    mapping = DeviceDatapointMap.objects.filter(device_id=device.pk).first()
    if mapping is None:
        return {}
    if require_confirmed:
        if mapping.status != DeviceDatapointMap.Status.CONFIRMED:
            return {}
        if not mapping.confirmed_checksum or mapping.confirmed_checksum != mapping_checksum(device, mapping.datapoints):
            return {}
    return datapoints_to_register_map(mapping.datapoints)


def datapoints_for_device(device, *, require_confirmed=True) -> list[dict]:
    if device_requires_mapping(device):
        mapping = DeviceDatapointMap.objects.filter(device_id=device.pk).first()
        if mapping is None:
            return []
        if require_confirmed and not effective_register_map(device, require_confirmed=True):
            return []
        return list(mapping.datapoints or [])
    return register_map_to_datapoints(device.template.register_map if device.template else {})


@transaction.atomic
def ensure_device_datapoint_map(device) -> DeviceDatapointMap:
    if not device_requires_mapping(device):
        raise ValidationError("This equipment uses a fixed Novena template and does not need a site map.")
    mapping, _ = DeviceDatapointMap.objects.get_or_create(
        team=device.team,
        device=device,
        defaults={"datapoints": register_map_to_datapoints(device.template.register_map)},
    )
    return mapping


@transaction.atomic
def reset_device_datapoint_map_for_template(*, device, team) -> DeviceDatapointMap:
    """A newly selected team map is a draft, even if this device had a confirmed map."""
    if device.team_id != team.pk or not device_requires_mapping(device):
        raise ValidationError("Choose a site-defined map for this team's equipment.")
    mapping = ensure_device_datapoint_map(device)
    mapping.datapoints = register_map_to_datapoints(device.template.register_map)
    mapping.status = DeviceDatapointMap.Status.DRAFT
    mapping.last_validation = {}
    mapping.datapoint_health = {}
    mapping.tested_checksum = ""
    mapping.confirmed_checksum = ""
    mapping.last_tested_at = None
    mapping.validated_by = None
    mapping.validated_at = None
    mapping.confirmed_by = None
    mapping.confirmed_at = None
    mapping.cloned_from = None
    mapping.save()
    return mapping


@transaction.atomic
def save_device_datapoint_map(*, device, team, datapoints) -> DeviceDatapointMap:
    if device.team_id != team.id:
        raise ValidationError("Equipment does not belong to this team.")
    normalized = normalize_datapoints(datapoints)
    mapping = ensure_device_datapoint_map(device)
    mapping.datapoints = normalized
    mapping.status = DeviceDatapointMap.Status.DRAFT
    mapping.last_validation = {}
    mapping.datapoint_health = {}
    mapping.tested_checksum = ""
    mapping.confirmed_checksum = ""
    mapping.last_tested_at = None
    mapping.validated_by = None
    mapping.validated_at = None
    mapping.confirmed_by = None
    mapping.confirmed_at = None
    mapping.save()
    metadata = dict(device.metadata or {})
    metadata["guided_setup_validation"] = "pending"
    device.metadata = metadata
    device.save(update_fields=["metadata", "updated_at"])
    return mapping


@transaction.atomic
def record_device_datapoint_validation(*, device, result, validated_by=None) -> DeviceDatapointMap:
    mapping = ensure_device_datapoint_map(device)
    tested_at = timezone.now()
    mapping.last_tested_at = tested_at
    mapping.last_validation = dict(result) if isinstance(result, dict) else {}
    signals = []
    for raw_signal in mapping.last_validation.get("signals") or []:
        signal = dict(raw_signal)
        signal["decoded_value"] = signal.get("decoded_value", signal.get("value"))
        signal["raw_value"] = signal.get("raw_value", signal.get("sample"))
        signal["warning_message"] = signal.get("warning_message", "")
        signal["error_message"] = signal.get("error_message", "")
        if not signal["error_message"] and not signal["warning_message"]:
            signal["error_message"] = signal.get("reason", "")
        signal["validated_at"] = tested_at.isoformat()
        signals.append(signal)
    mapping.last_validation["signals"] = signals
    mapped_keys = {point.get("key") for point in mapping.datapoints}
    mapping.datapoint_health = {
        signal["key"]: {
            "status": signal.get("status", "failed"),
            "decoded_value": signal.get("decoded_value", signal.get("value")),
            "raw_value": signal.get("raw_value", signal.get("sample")),
            "error_message": signal.get("error_message", signal.get("reason", "")),
            "warning_message": signal.get("warning_message", ""),
            "validated_at": tested_at.isoformat(),
        }
        for signal in signals
        if signal.get("key") in mapped_keys
    }
    returned_keys = [signal.get("key") for signal in signals]
    successful = (
        bool(signals)
        and len(signals) == len(mapping.datapoints)
        and len(returned_keys) == len(set(returned_keys))
        and set(returned_keys) == mapped_keys
        and all(signal.get("status") in {"success", "warning"} and not signal.get("blocking") for signal in signals)
    )
    current_checksum = mapping_checksum(device, mapping.datapoints)
    tested_checksum = str(mapping.last_validation.get("mapping_checksum") or "")
    if mapping.last_validation.get("status") == "success" and successful and tested_checksum == current_checksum:
        mapping.status = DeviceDatapointMap.Status.AWAITING_CONFIRMATION
        mapping.tested_checksum = tested_checksum
        mapping.validated_by = validated_by if getattr(validated_by, "is_authenticated", False) else None
        mapping.validated_at = tested_at
    else:
        mapping.status = DeviceDatapointMap.Status.NEEDS_ATTENTION
        mapping.tested_checksum = ""
        mapping.validated_by = None
        mapping.validated_at = None
    mapping.confirmed_checksum = ""
    mapping.confirmed_by = None
    mapping.confirmed_at = None
    mapping.save()
    return mapping


@transaction.atomic
def confirm_device_datapoint_map(*, device, team, confirmed_by) -> DeviceDatapointMap:
    if device.team_id != team.id:
        raise ValidationError("Equipment does not belong to this team.")
    mapping = ensure_device_datapoint_map(device)
    mapping = DeviceDatapointMap.objects.select_for_update().get(pk=mapping.pk)
    current_checksum = mapping_checksum(device, mapping.datapoints)
    if (
        mapping.status == DeviceDatapointMap.Status.CONFIRMED
        and mapping.confirmed_checksum == current_checksum
    ):
        # The mapping page and guided onboarding can both ask the customer to
        # confirm the same tested map. Repeating that confirmation is safe.
        return mapping
    if mapping.status != DeviceDatapointMap.Status.AWAITING_CONFIRMATION:
        raise ValidationError("Run a successful live validation before confirming these signals.")
    if not mapping.tested_checksum or mapping.tested_checksum != current_checksum:
        raise ValidationError("Connection or signal settings changed after the live test. Test them again.")
    mapping.status = DeviceDatapointMap.Status.CONFIRMED
    mapping.confirmed_checksum = current_checksum
    mapping.confirmed_by = confirmed_by if getattr(confirmed_by, "is_authenticated", False) else None
    mapping.confirmed_at = timezone.now()
    mapping.save()
    metadata = dict(device.metadata or {})
    metadata["guided_setup_validation"] = "validated"
    device.metadata = metadata
    device.save(update_fields=["metadata", "updated_at"])
    latest = mapping.revisions.order_by("-revision_number").first()
    current_datapoints_checksum = datapoints_checksum(mapping.datapoints)
    if (
        latest is None
        or latest.datapoints_checksum != current_datapoints_checksum
        or latest.confirmed_checksum != mapping.confirmed_checksum
    ):
        DeviceDatapointMapRevision.objects.create(
            team=team,
            mapping=mapping,
            revision_number=(latest.revision_number + 1) if latest else 1,
            datapoints=mapping.datapoints,
            datapoints_checksum=current_datapoints_checksum,
            confirmed_checksum=mapping.confirmed_checksum,
            validation_result=mapping.last_validation,
            validated_by=mapping.validated_by,
            validated_at=mapping.validated_at,
            confirmed_by=mapping.confirmed_by,
            confirmed_at=mapping.confirmed_at,
        )
    return mapping


@transaction.atomic
def rollback_device_datapoint_map(*, device, team, revision) -> DeviceDatapointMap:
    if device.team_id != team.id or revision.team_id != team.id:
        raise ValidationError("Mappings can only be restored within the current team.")
    mapping = ensure_device_datapoint_map(device)
    if revision.mapping_id != mapping.id:
        raise ValidationError("Choose a revision for this equipment.")
    return save_device_datapoint_map(device=device, team=team, datapoints=revision.datapoints)


@transaction.atomic
def clone_device_datapoint_map(*, source_device, target_device, team) -> DeviceDatapointMap:
    if source_device.team_id != team.id or target_device.team_id != team.id:
        raise ValidationError("Mappings can only be cloned within the current team.")
    if source_device.protocol != target_device.protocol:
        raise ValidationError("Choose equipment using the same protocol.")
    source = ensure_device_datapoint_map(source_device)
    if source.status != DeviceDatapointMap.Status.CONFIRMED:
        raise ValidationError("Only a confirmed mapping can be cloned.")
    target = save_device_datapoint_map(device=target_device, team=team, datapoints=source.datapoints)
    target.cloned_from = source
    target.save(update_fields=["cloned_from", "updated_at"])
    return target
