import logging
import math
from datetime import datetime, time

from django.utils import timezone

from apps.devices.models import Device
from apps.devices.services import current_claimed_gateway
from apps.utils.timezones import format_site_datetime, get_site_timezone

from .models import TelemetryData

logger = logging.getLogger("novena_hub")


def ingest_telemetry_data(gateway_sn, values, timestamp=None, device_id=None):
    """
    Ingests telemetry data from a gateway.
    Matches by device ID first, then exact device name, with legacy fallback logged.
    """
    cloud_received_at = timezone.now()
    if timestamp is None:
        timestamp = cloud_received_at

    gateway = current_claimed_gateway(gateway_sn)
    if not gateway:
        logger.warning("Gateway with SN %s not found. Ingestion skipped.", gateway_sn)
        return

    gateway.last_seen = cloud_received_at
    gateway.status = "online"
    gateway.save(update_fields=["last_seen", "status"])

    target_device = None
    values = dict(values)
    device_name = values.pop("device_name", None)

    if device_id:
        try:
            target_device = gateway.devices.filter(pk=int(device_id)).first()
        except (TypeError, ValueError):
            logger.warning("Invalid telemetry device_id %s from gateway %s.", device_id, gateway_sn)
        if not target_device:
            logger.debug("device_id %s not found on gateway %s, falling back to name match", device_id, gateway_sn)

    if not target_device and device_name:
        try:
            target_device = gateway.devices.get(name=device_name)
        except Device.DoesNotExist:
            logger.info("Device %s not found for gateway %s.", device_name, gateway_sn)

    legacy_fallback_used = False
    if not target_device:
        legacy_candidates = list(gateway.devices.order_by("id")[:2])
        if len(legacy_candidates) == 1:
            target_device = legacy_candidates[0]
            legacy_fallback_used = True
        elif len(legacy_candidates) > 1:
            logger.warning(
                "Rejected ambiguous telemetry for gateway %s. Payload device_id=%s device_name=%s; "
                "the gateway has multiple configured devices.",
                gateway_sn,
                device_id,
                device_name,
            )

    if legacy_fallback_used:
        logger.warning(
            "Using legacy first-device telemetry fallback for gateway %s (single configured device). "
            "Payload device_id=%s device_name=%s resolved_device=%s.",
            gateway_sn,
            device_id,
            device_name,
            target_device.id,
        )

    if not target_device:
        logger.warning("No target device identified for telemetry from gateway %s", gateway_sn)
        return

    db_flushed_at = timezone.now()
    telemetry_objects = []
    for key, value in values.items():
        data_point = TelemetryData(
            device=target_device,
            timestamp=timestamp,
            cloud_received_at=cloud_received_at,
            db_flushed_at=db_flushed_at,
            key=key,
        )

        if isinstance(value, bool):
            data_point.value_bool = value
        elif isinstance(value, int | float):
            data_point.value_numeric = float(value)
        else:
            data_point.value_string = str(value)

        telemetry_objects.append(data_point)

    if telemetry_objects:
        TelemetryData.objects.bulk_create(telemetry_objects)

        from apps.alerts.services import check_alerts_for_payload

        for key, value in values.items():
            check_alerts_for_payload(target_device, key, value)

        from apps.automations.engine import evaluate_automations

        evaluate_automations(target_device, values)

        target_device.last_telemetry_at = timestamp
        if target_device.status != "alarm":
            target_device.status = "online"
            target_device.save(update_fields=["last_telemetry_at", "status"])
        else:
            target_device.save(update_fields=["last_telemetry_at"])

        logger.info("Ingested %d points for device %s", len(telemetry_objects), target_device.name)

    return target_device


def get_latest_telemetry_for_chart(device, key, limit=20):
    """
    Fetches the latest data points for a specific key/device combination.
    Returns lists of timestamps and values formatted for Chart.js.
    """
    data = TelemetryData.objects.filter(device=device, key=key).order_by("-timestamp")[:limit]

    # Reverse to get chronological order (evaluate queryset to list first)
    data = list(reversed(list(data)))

    labels = []
    values = []

    for point in data:
        labels.append(format_site_datetime(point.timestamp, device.site, "%H:%M:%S"))
        values.append(point.value_numeric or 0)

    return {"labels": labels, "values": values, "key": key}


def get_latest_telemetry_value(device, key):
    """Fetches the single most recent data point for a key."""
    point = TelemetryData.objects.filter(device=device, key=key).order_by("-timestamp").first()

    if point:
        if point.value_numeric is not None:
            return point.value_numeric
        return point.value_string or point.value_bool
    return None


def get_site_summary_stats(site):
    """Site-local observed import energy and fresh mapped demand.

    Missing, stale, reset, or gapped counters never become a plausible zero.
    """
    from apps.devices.datapoint_maps import effective_register_map

    now = timezone.now()
    local_now = timezone.localtime(now, get_site_timezone(site))
    day_start = datetime.combine(local_now.date(), time.min, tzinfo=get_site_timezone(site))
    devices = list(site.devices.select_related("template"))
    total_kwh = 0.0
    total_power = 0.0
    energy_coverage = []
    energy_last_samples = []
    power_last_samples = []
    energy_missing = False
    power_missing = False
    energy_meters = 0
    power_meters = 0

    for device in devices:
        mapping = effective_register_map(device)
        energy_key = next(
            (
                key
                for key in ("energy_import", "import_energy", "energy_import_total")
                if mapping.get(key, {}).get("unit") in ("Wh", "kWh", "MWh")
            ),
            None,
        )
        power_key = next(
            (
                key
                for key in ("active_power_total", "active_power", "power")
                if mapping.get(key, {}).get("unit") in ("W", "kW")
            ),
            None,
        )
        if energy_key:
            energy_meters += 1
            points = list(
                TelemetryData.objects.filter(
                    device=device,
                    key=energy_key,
                    timestamp__gte=day_start,
                    timestamp__lte=now,
                    value_numeric__isnull=False,
                )
                .order_by("timestamp")
                .values_list("timestamp", "value_numeric")
            )
            if (
                len(points) < 2
                or (points[0][0] - day_start).total_seconds() > 300
                or (now - points[-1][0]).total_seconds() > 300
            ):
                energy_missing = True
            else:
                gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(points, points[1:], strict=False)]
                values = [value for _, value in points]
                if (
                    max(gaps) > 1800
                    or any(not math.isfinite(value) or value < 0 for value in values)
                    or any(b < a for a, b in zip(values, values[1:], strict=False))
                ):
                    energy_missing = True
                else:
                    unit_factor = {"Wh": 0.001, "kWh": 1, "MWh": 1000}[mapping[energy_key]["unit"]]
                    total_kwh += (values[-1] - values[0]) * unit_factor
                    energy_coverage.append(
                        (points[-1][0] - points[0][0]).total_seconds() / max((now - day_start).total_seconds(), 1)
                    )
                    energy_last_samples.append(points[-1][0])
        elif power_key:
            # Power alone does not prove day-long consumption.
            energy_missing = True

        if power_key:
            power_meters += 1
            point = (
                TelemetryData.objects.filter(
                    device=device, key=power_key, timestamp__lte=now, value_numeric__isnull=False
                )
                .order_by("-timestamp")
                .first()
            )
            if not point or (now - point.timestamp).total_seconds() > 300 or not math.isfinite(point.value_numeric):
                power_missing = True
            else:
                total_power += point.value_numeric * (1000 if mapping[power_key]["unit"] == "kW" else 1)
                power_last_samples.append(point.timestamp)

    coverage = min(energy_coverage) if energy_coverage else 0.0
    daily_kwh = total_kwh if energy_meters and not energy_missing and coverage >= 0.9 else None
    current_power = total_power if power_meters and not power_missing else None
    return {
        "daily_kwh": daily_kwh,
        "current_power": current_power,
        "energy_coverage_percent": round(coverage * 100),
        "energy_last_sample_at": min(energy_last_samples) if daily_kwh is not None else None,
        "power_last_sample_at": min(power_last_samples) if current_power is not None else None,
        "energy_status": "observed" if daily_kwh is not None else "unavailable",
    }
