import logging
from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.devices.freshness import device_freshness_thresholds
from apps.telemetry.models import TelemetryData

from .models import Alert, AlertRule

logger = logging.getLogger("novena_hub")


def check_alerts_for_payload(device, key, value):
    """
    Checks all active alert rules for a specific telemetry data point.
    """
    if not isinstance(value, int | float):
        return

    # Find rules that apply to this device and key
    # or rules that apply to the site and key
    rules = AlertRule.objects.filter(is_active=True, telemetry_key=key).filter(
        models.Q(device=device) | models.Q(device__isnull=True, site=device.site)
    )

    for rule in rules:
        if evaluate_condition(rule, device, key, value):
            trigger_alert(rule, device, value)
        else:
            resolve_alert(rule, device, value)


def evaluate_condition(rule, device, key, value):
    """Evaluates if the telemetry value meets the rule threshold, taking duration into account."""
    # First check the current value
    if not evaluate_condition_value(rule, value):
        return False

    if rule.duration_seconds <= 0:
        return True

    # Check historical values in duration window
    window_start = timezone.now() - timedelta(seconds=rule.duration_seconds)
    points = list(
        TelemetryData.objects.filter(device=device, key=key, timestamp__gte=window_start)
        .order_by("timestamp")
    )

    if not points:
        return False

    oldest_in_window = points[0]
    # If the oldest telemetry point in the window is newer than window_start + tolerance,
    # we must check if there is an out-of-bounds point immediately before the window.
    if oldest_in_window.timestamp > window_start + timedelta(seconds=2):
        prev_point = (
            TelemetryData.objects.filter(device=device, key=key, timestamp__lt=window_start)
            .order_by("-timestamp")
            .first()
        )

        if not prev_point:
            return False

        prev_value = _condition_value(prev_point)
        if prev_value is None or not evaluate_condition_value(rule, prev_value):
            return False
        points.insert(0, prev_point)

    # A disconnected sensor cannot establish that a limit stayed exceeded. Use
    # the same per-device offline threshold as the monitoring UI so a long gap
    # breaks the duration window, including the gap before the first new sample.
    _, max_gap_seconds = device_freshness_thresholds(device)
    max_gap = timedelta(seconds=max_gap_seconds)
    if timezone.now() - points[-1].timestamp > max_gap:
        return False
    return all(
        _condition_value(point) is not None
        and evaluate_condition_value(rule, _condition_value(point))
        and (index == 0 or point.timestamp - points[index - 1].timestamp <= max_gap)
        for index, point in enumerate(points)
    )


def _condition_value(point):
    if point.value_numeric is not None:
        return point.value_numeric
    if point.value_bool is not None:
        return int(point.value_bool)
    return None


def evaluate_condition_value(rule, value):
    """Evaluates if a single telemetry value meets the threshold."""
    if rule.condition == "gt":
        return value > rule.threshold
    elif rule.condition == "lt":
        return value < rule.threshold
    elif rule.condition == "gte":
        return value >= rule.threshold
    elif rule.condition == "lte":
        return value <= rule.threshold
    elif rule.condition == "eq":
        return value == rule.threshold
    elif rule.condition == "neq":
        return value != rule.threshold
    return False


def trigger_alert(rule, device, value):
    """Creates an alert if not recently triggered (cooldown)."""
    cooldown_limit = timezone.now() - timedelta(minutes=rule.cooldown_minutes)

    # Cooldown Hardening: include both "active" and "acknowledged" alerts
    recent_alert = Alert.objects.filter(
        rule=rule, device=device, status__in=["active", "acknowledged"], triggered_at__gte=cooldown_limit
    ).exists()

    if not recent_alert:
        alert = Alert.objects.create(team=rule.team, rule=rule, device=device, trigger_value=value, status="active")
        logger.warning(f"ALERT TRIGGERED: {rule.name} for device {device.name} (Value: {value})")

        # Send email/webhook notifications
        from .notifications import send_alert_notifications

        send_alert_notifications(alert)

        # Auto-create maintenance ticket if applicable
        from apps.maintenance.services import auto_create_ticket

        auto_create_ticket(alert)


def resolve_alert(rule, device, value):
    """Marks any active or acknowledged alerts for this rule as resolved."""
    active_alerts = Alert.objects.filter(rule=rule, device=device, status__in=["active", "acknowledged"])
    for alert in active_alerts:
        alert.status = "resolved"
        alert.resolved_at = timezone.now()
        alert.save(update_fields=["status", "resolved_at"])
        logger.info(f"ALERT RESOLVED: {rule.name} for device {device.name} (Value: {value})")

        # Send resolution notifications
        from .notifications import send_alert_resolution_notifications

        send_alert_resolution_notifications(alert)
