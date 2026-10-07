import math
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

DEFAULT_POLLING_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class FreshnessState:
    status: str
    label: str
    display: str
    age_seconds: int | None = None

    @property
    def tone(self):
        return {
            "live": "green",
            "delayed": "amber",
            "offline": "gray",
            "alarm": "red",
            "maintenance": "amber",
        }.get(self.status, "gray")


def compact_timesince(dt, now=None):
    if not dt:
        return None

    now = now or timezone.now()
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())

    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def expected_device_interval_seconds(device):
    from apps.subscriptions.enforcement import get_effective_polling_interval_seconds

    try:
        return max(1, math.ceil(get_effective_polling_interval_seconds(device)))
    except (TypeError, ValueError):
        return DEFAULT_POLLING_INTERVAL_SECONDS


def device_freshness_thresholds(device):
    expected_interval = expected_device_interval_seconds(device)
    delayed_multiplier = getattr(settings, "DEVICE_DELAYED_MULTIPLIER", 2)
    offline_multiplier = getattr(settings, "DEVICE_OFFLINE_MULTIPLIER", 3)
    offline_min = getattr(settings, "DEVICE_OFFLINE_MIN_SECONDS", 30)

    delayed_seconds = max(expected_interval * delayed_multiplier, expected_interval + 10)
    offline_seconds = max(offline_min, expected_interval * offline_multiplier)
    return int(delayed_seconds), int(offline_seconds)


def device_freshness_state(device, now=None):
    now = now or timezone.now()
    last_seen = getattr(device, "last_telemetry_at", None)

    if getattr(device, "status", None) == "alarm":
        if last_seen:
            return FreshnessState(
                status="alarm",
                label="Alarm",
                display=f"Alarm - last sample {compact_timesince(last_seen, now)} ago",
                age_seconds=int((now - last_seen).total_seconds()),
            )
        return FreshnessState(status="alarm", label="Alarm", display="Alarm - no samples yet")

    if not last_seen:
        return FreshnessState(status="offline", label="Offline", display="Offline - no samples yet")

    age_seconds = max(0, int((now - last_seen).total_seconds()))
    delayed_seconds, offline_seconds = device_freshness_thresholds(device)

    if age_seconds >= offline_seconds:
        return FreshnessState(
            status="offline",
            label="Offline",
            display=f"Offline - last sample {compact_timesince(last_seen, now)} ago",
            age_seconds=age_seconds,
        )
    if age_seconds >= delayed_seconds:
        return FreshnessState(
            status="delayed",
            label="Delayed",
            display=f"Delayed - last sample {compact_timesince(last_seen, now)} ago",
            age_seconds=age_seconds,
        )
    return FreshnessState(
        status="live",
        label="Live",
        display=f"Live - updated {compact_timesince(last_seen, now)} ago",
        age_seconds=age_seconds,
    )


def gateway_freshness_state(gateway, now=None):
    now = now or timezone.now()
    last_seen = getattr(gateway, "last_seen", None)
    raw_status = getattr(gateway, "status", None)

    if raw_status == "maintenance":
        if last_seen:
            return FreshnessState(
                status="maintenance",
                label="Gateway maintenance",
                display=f"Gateway maintenance - heartbeat {compact_timesince(last_seen, now)} ago",
                age_seconds=int((now - last_seen).total_seconds()),
            )
        return FreshnessState(
            status="maintenance",
            label="Gateway maintenance",
            display="Gateway maintenance - no heartbeat yet",
        )

    if last_seen:
        age_seconds = max(0, int((now - last_seen).total_seconds()))
        gateway_timeout = getattr(settings, "GATEWAY_OFFLINE_SECONDS", 120)
        if raw_status == "online" and age_seconds <= gateway_timeout:
            return FreshnessState(
                status="live",
                label="Gateway online",
                display=f"Gateway online - heartbeat {compact_timesince(last_seen, now)} ago",
                age_seconds=age_seconds,
            )
        if raw_status == "offline" and not gateway.firmware_version:
            return FreshnessState(
                status="offline",
                label="Gateway offline",
                display="Gateway offline - operational heartbeat missing",
                age_seconds=age_seconds,
            )
        return FreshnessState(
            status="offline",
            label="Gateway offline",
            display=f"Gateway offline - last heartbeat {compact_timesince(last_seen, now)} ago",
            age_seconds=age_seconds,
        )

    return FreshnessState(
        status="offline", label="Gateway offline", display="Gateway offline - no operational heartbeat yet"
    )


def device_offline_cutoff(device, now=None):
    now = now or timezone.now()
    _, offline_seconds = device_freshness_thresholds(device)
    return now - timedelta(seconds=offline_seconds)


def device_gateway_context_display(device):
    gateway = getattr(device, "gateway", None)
    if not gateway:
        return ""

    device_state = device_freshness_state(device)
    gateway_state = gateway_freshness_state(gateway)
    if device_state.status == "offline" and gateway_state.status == "live":
        return "Gateway online - device offline"
    return gateway_state.display
