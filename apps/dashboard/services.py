import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from apps.dashboard.models import Dashboard, Widget
from apps.users.models import CustomUser

ENERGY_KEYS = {"active_power", "energy", "voltage", "current", "power_factor", "frequency"}
MOTOR_KEYS = {"run_status", "speed", "motor_speed", "output_frequency", "frequency_command", "fault", "fault_code"}
PUMP_KEYS = {"pressure", "flow", "flow_rate", "pump_status", "pump_fault"}
PLC_KEYS = {"plc_status", "alarm", "fault", "process_value", "setpoint", "input", "output"}
COLD_CHAIN_KEYS = {"temperature", "humidity", "door_status", "door_open", "compressor_status"}
PROCESS_TREND_KEYS = [
    "active_power",
    "energy",
    "output_frequency",
    "frequency",
    "speed",
    "pressure",
    "flow_rate",
    "flow",
    "temperature",
    "humidity",
    "current",
    "voltage",
]
WIDGET_TREND_KEYS = {"active_power", "energy", "output_frequency", "frequency_command"}
UNIT_LIMITS = {
    "V": {"min": 0, "max": 500},
    "A": {"min": 0, "max": 100},
    "degC": {"min": 0, "max": 120},
    "bar": {"min": 0, "max": 16},
    "%": {"min": 0, "max": 100},
    "Hz": {"min": 0, "max": 60},
}


def get_user_signups(start: datetime.date | None = None, end: datetime.date | None = None, include_unconfirmed=None):
    extra_filter_kwargs = {}
    if include_unconfirmed is None:
        include_unconfirmed = settings.ACCOUNT_EMAIL_VERIFICATION != "mandatory"
    if not include_unconfirmed:
        extra_filter_kwargs = {"emailaddress__verified": True}
    end = end or timezone.now()
    start = start or end - datetime.timedelta(days=90)
    data = (
        CustomUser.objects.filter(date_joined__gte=start, date_joined__lte=end, **extra_filter_kwargs)
        .annotate(date=TruncDate("date_joined"))
        .values("date")
        .annotate(count=Count("id"))
        .order_by("date")
    )
    return data


def _register_items(device, include_writable_display=False):
    from apps.devices.datapoint_maps import effective_register_map

    register_map = effective_register_map(device)
    if not register_map:
        return []
    from apps.devices.solution_profiles import profile_key_order

    profile_priority = profile_key_order(device.site)
    items = []
    for key, config in register_map.items():
        if not isinstance(config, dict):
            continue
        writable = bool(config.get("writable"))
        role = config.get("dashboard_role", "control" if writable else "secondary")
        if (
            writable
            and not include_writable_display
            and not config.get("display")
            and not config.get("display_type")
            and role not in {"primary", "trend", "health", "secondary"}
        ):
            continue
        items.append(
            {
                "key": key,
                "label": config.get("label", key.replace("_", " ").title()),
                "unit": config.get("unit", ""),
                "type": config.get("type", "uint16"),
                "priority": int(config.get("priority", 50)),
                "dashboard_role": role,
                "writable": writable,
                "min": config.get("min"),
                "max": config.get("max"),
                "normal_min": config.get("normal_min"),
                "normal_max": config.get("normal_max"),
                "address": config.get("address", 0),
                "functionCode": config.get("functionCode", 3),
                "objectsCount": config.get("objectsCount", 1),
                "control": config.get("control", "input"),
                "labels": config.get("labels", ["OFF", "ON"]),
                "config": config,
            }
        )
    return sorted(items, key=lambda item: (profile_priority.get(item["key"], 100), item["priority"], item["label"]))


def _latest_point(device, key=None):
    from apps.telemetry.models import TelemetryData

    qs = TelemetryData.objects.filter(device=device)
    if key:
        qs = qs.filter(key=key)
    return qs.order_by("-timestamp").first()


def _point_value(point):
    if not point:
        return None
    if point.value_numeric is not None:
        return point.value_numeric
    if point.value_bool is not None:
        return point.value_bool
    return point.value_string


def _format_value(value):
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _display_unit(unit):
    return {"degC": "°C", "degF": "°F"}.get(unit, unit)


def _recent_keys(device):
    from apps.telemetry.models import TelemetryData

    return set(TelemetryData.objects.filter(device=device).order_by().values_list("key", flat=True).distinct()[:20])


def _device_keys(device):
    keys = {item["key"] for item in _register_items(device, include_writable_display=True)}
    if not keys:
        keys = _recent_keys(device)
    return keys


def classify_device(device):
    keys = _device_keys(device)
    device_type = device.device_type or "other"
    category = getattr(device.template, "category", "") if device.template else ""
    if device_type == "vfd" or keys & MOTOR_KEYS:
        return {"type": "motor", "label": "Motor / VFD", "keys": keys}
    if keys & PUMP_KEYS:
        return {"type": "pump", "label": "Pump", "keys": keys}
    if device_type == "plc" or keys & PLC_KEYS:
        return {"type": "plc", "label": "PLC", "keys": keys}
    if device_type in {"temp_sensor", "chiller"} or category == "cold_chain" or keys & COLD_CHAIN_KEYS:
        return {"type": "temperature", "label": "Temperature", "keys": keys}
    if device_type in {"power_meter", "solar_inverter"} or category == "energy" or keys & ENERGY_KEYS:
        return {"type": "energy", "label": "Energy", "keys": keys}
    return {"type": "generic", "label": "General", "keys": keys}


def _widget_definition(device, key, config):
    label = config.get("label", key.replace("_", " ").title())
    unit = config.get("unit", "")
    reg_type = str(config.get("type", "uint16")).lower()
    role = config.get("dashboard_role", "")
    key_lower = key.lower()
    width = 3
    height = 3
    widget_config = {}

    explicit_display = config.get("display_type")
    if explicit_display == "trend":
        widget_type = "timeseries"
        width = 6
        height = 4
    elif explicit_display == "status":
        widget_type = "indicator"
    elif explicit_display == "gauge":
        widget_type = "gauge"
    elif explicit_display == "value":
        widget_type = "value"
    elif role == "trend" or key_lower in WIDGET_TREND_KEYS:
        widget_type = "timeseries"
        width = 6
        height = 4
    elif reg_type in {"bool", "bits"} or "status" in key_lower or "alarm" in key_lower or "fault" in key_lower:
        widget_type = "indicator"
    elif (
        unit in UNIT_LIMITS
        or unit == "°C"
        or any(
            token in key_lower
            for token in ["voltage", "current", "temperature", "pressure", "flow", "speed", "frequency"]
        )
    ):
        widget_type = "gauge"
    else:
        widget_type = "value"

    if role == "primary" and widget_type == "value":
        width = 4
    if widget_type == "gauge":
        default_limits = UNIT_LIMITS.get(unit, UNIT_LIMITS.get("degC") if unit == "°C" else {"min": 0, "max": 100})
        widget_config = {
            "min": config.get("min", default_limits["min"]),
            "max": config.get("max", default_limits["max"]),
        }
        if config.get("normal_min") is not None:
            widget_config["normal_min"] = config["normal_min"]
        if config.get("normal_max") is not None:
            widget_config["normal_max"] = config["normal_max"]

    return label, unit, widget_type, width, height, widget_config


def generate_default_dashboard(device):
    """Generate a default dashboard from DeviceTemplate register-map hints."""
    if not _register_items(device):
        return None

    dashboard, created = Dashboard.objects.get_or_create(
        team=device.team,
        device=device,
        defaults={"name": f"{device.name} Overview", "site": device.site, "is_default": True},
    )
    if not created:
        dashboard.widgets.all().delete()

    row_idx = 0
    col_idx = 0
    for item in _register_items(device):
        key = item["key"]
        label, unit, widget_type, width, height, widget_config = _widget_definition(device, key, item["config"])
        Widget.objects.create(
            team=device.team,
            dashboard=dashboard,
            device=device,
            title=label,
            widget_type=widget_type,
            telemetry_key=key,
            unit=unit,
            row=row_idx,
            col=col_idx,
            width=width,
            height=height,
            config=widget_config,
        )
        col_idx += width
        if col_idx >= 12:
            col_idx = 0
            row_idx += 1
    return dashboard


def _latest_readings_for_device(device, limit=4):
    items = _register_items(device)[:limit]
    readings = []
    if items:
        for item in items:
            point = _latest_point(device, item["key"])
            value = _point_value(point)
            readings.append(
                {
                    "key": item["key"],
                    "label": item["label"],
                    "unit": _display_unit(item["unit"]),
                    "value": value,
                    "display_value": _format_value(value),
                    "timestamp": point.timestamp if point else None,
                }
            )
        return readings

    point = _latest_point(device)
    if point:
        value = _point_value(point)
        readings.append(
            {
                "key": point.key,
                "label": point.key.replace("_", " ").title(),
                "unit": "",
                "value": value,
                "display_value": _format_value(value),
                "timestamp": point.timestamp,
            }
        )
    return readings


def _trend_candidates(devices):
    candidates = []
    for device in devices:
        classification = classify_device(device)
        from apps.devices.solution_profiles import profile_key_order

        profile_priority = profile_key_order(device.site)
        items = _register_items(device)
        if items:
            for item in items:
                if item["key"] in PROCESS_TREND_KEYS or item["dashboard_role"] == "trend":
                    candidates.append((device, item, classification, profile_priority.get(item["key"], 100)))
        else:
            for key in _recent_keys(device):
                if key in PROCESS_TREND_KEYS:
                    candidates.append(
                        (
                            device,
                            {"key": key, "label": key.replace("_", " ").title(), "unit": ""},
                            classification,
                            profile_priority.get(key, 100),
                        )
                    )
    priority = {key: idx for idx, key in enumerate(PROCESS_TREND_KEYS)}
    candidates.sort(key=lambda row: (row[3], priority.get(row[1]["key"], 99)))
    return candidates


def _build_operations_trend(team, devices):
    from apps.telemetry.models import TelemetryData

    cache_key = f"dashboard_{team.id}_operations_trend_v2"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    now = timezone.now()
    start = now - datetime.timedelta(hours=24)
    selected = None
    for device, item, classification, _profile_priority in _trend_candidates(devices):
        if TelemetryData.objects.filter(
            device=device, key=item["key"], value_numeric__isnull=False, timestamp__gte=start
        ).exists():
            selected = (device, item, classification)
            break

    if not selected:
        trend = {
            "title": "Recent Telemetry Activity",
            "subtitle": "No numeric trend is available yet.",
            "key": "",
            "unit": "",
            "labels": [],
            "values": [],
            "empty": True,
        }
        cache.set(cache_key, trend, 60)
        return trend

    device, item, _classification = selected
    try:
        site_timezone = ZoneInfo(device.site.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        site_timezone = timezone.get_current_timezone()
    labels = []
    values = []
    cumulative = item["key"] == "energy"
    for i in range(23, -1, -1):
        hour_start = now - datetime.timedelta(hours=i + 1)
        hour_end = now - datetime.timedelta(hours=i)
        points = TelemetryData.objects.filter(
            device=device,
            key=item["key"],
            value_numeric__isnull=False,
            timestamp__gte=hour_start,
            timestamp__lt=hour_end,
        )
        value = (
            points.order_by("-timestamp").values_list("value_numeric", flat=True).first()
            if cumulative
            else points.aggregate(average=Avg("value_numeric"))["average"]
        )
        labels.append(timezone.localtime(hour_end, site_timezone).strftime("%H:%M"))
        values.append(round(float(value), 2) if value is not None else None)

    observed = [value for value in values if value is not None]

    trend = {
        "title": "Operations Trend",
        "subtitle": f"{item['label']} · {device.name} · hourly {'last reading' if cumulative else 'average'}",
        "key": item["key"],
        "unit": _display_unit(item.get("unit", "")),
        "time_zone": str(site_timezone),
        "labels": labels,
        "values": values,
        "current": observed[-1] if observed else None,
        "minimum": min(observed) if observed else None,
        "maximum": max(observed) if observed else None,
        "empty": False,
    }
    cache.set(cache_key, trend, 60)
    return trend


def _build_attention_items(team, devices, gateways, active_alerts, open_tickets, overdue_pms, active_rule_count):
    items = []
    for device in devices:
        state = device.freshness
        gateway_state = device.gateway.freshness if device.gateway else None
        if state.status == "alarm":
            items.append(
                {
                    "tone": "red",
                    "title": device.name,
                    "message": state.display,
                    "kind": "Device alarm",
                    "device": device,
                    "occurred_at": device.last_telemetry_at,
                    "action_url": reverse("web_team:devices:device_detail", args=[team.slug, device.pk]),
                    "action_label": "Inspect device",
                }
            )
        elif state.status == "delayed":
            items.append(
                {
                    "tone": "amber",
                    "title": device.name,
                    "message": state.display,
                    "kind": "Field device",
                    "device": device,
                    "occurred_at": device.last_telemetry_at,
                    "action_url": reverse("web_team:devices:device_detail", args=[team.slug, device.pk]),
                    "action_label": "Inspect device",
                }
            )
        elif state.status == "offline":
            items.append(
                {
                    "tone": "amber",
                    "title": device.name,
                    "message": "Gateway online - device offline"
                    if gateway_state and gateway_state.status == "live"
                    else state.display,
                    "kind": "Field device",
                    "device": device,
                    "occurred_at": device.last_telemetry_at,
                    "action_url": reverse("web_team:devices:device_detail", args=[team.slug, device.pk]),
                    "action_label": "Inspect device",
                }
            )
    if devices and active_rule_count == 0:
        items.append(
            {
                "tone": "amber",
                "title": "Alert monitoring not set up",
                "message": "No active monitoring rules are checking equipment thresholds yet.",
                "kind": "Monitoring setup",
                "occurred_at": None,
                "action_url": reverse("web_team:alerts:rule_list", args=[team.slug]),
                "action_label": "Review suggested rules",
            }
        )
    for gateway in gateways:
        state = gateway.freshness
        if state.status == "offline":
            items.append(
                {
                    "tone": "gray",
                    "title": gateway.name,
                    "message": state.display,
                    "kind": "Gateway",
                    "gateway": gateway,
                    "occurred_at": gateway.last_seen,
                    "action_url": reverse("web_team:devices:gateway_detail", args=[team.slug, gateway.pk]),
                    "action_label": "Inspect gateway",
                }
            )
    for alert in active_alerts[:5]:
        items.append(
            {
                "tone": "red",
                "title": alert.rule.name,
                "message": f"{alert.device.name} triggered {alert.trigger_value:g}",
                "kind": "Alert",
                "alert": alert,
                "device": alert.device,
                "occurred_at": alert.triggered_at,
                "action_url": reverse("web_team:alerts:alert_list", args=[team.slug]),
                "action_label": "Open alerts",
            }
        )
    if overdue_pms:
        items.append(
            {
                "tone": "amber",
                "title": "Preventive maintenance overdue",
                "message": f"{overdue_pms} schedule needs attention",
                "kind": "Maintenance",
                "occurred_at": None,
                "action_url": reverse("web_team:maintenance:schedule_list", args=[team.slug]),
                "action_label": "Review schedules",
            }
        )
    if open_tickets:
        items.append(
            {
                "tone": "blue",
                "title": "Open maintenance work",
                "message": f"{open_tickets} ticket(s) in progress",
                "kind": "Maintenance",
                "occurred_at": None,
                "action_url": reverse("web_team:maintenance:ticket_list", args=[team.slug]),
                "action_label": "Open tickets",
            }
        )
    return items[:8]


def build_team_operations_dashboard(team, *, include_impact=False, impact_site_ids=None, visible_site_ids=None):
    from apps.alerts.models import Alert, AlertRule
    from apps.automations.models import Automation
    from apps.devices.models import Device, Gateway, Site
    from apps.events.models import ActivityLog
    from apps.maintenance.models import MaintenanceTicket, PreventiveSchedule

    site_qs = Site.objects.filter(team=team)
    device_qs = Device.objects.filter(team=team)
    gateway_qs = Gateway.objects.filter(team=team)
    if visible_site_ids is not None:
        site_qs = site_qs.filter(id__in=visible_site_ids)
        device_qs = device_qs.filter(site_id__in=visible_site_ids)
        gateway_qs = gateway_qs.filter(site_id__in=visible_site_ids)
    sites = list(site_qs.order_by("name"))
    devices = list(device_qs.select_related("site", "gateway", "template").order_by("-last_telemetry_at", "name"))
    gateways = list(gateway_qs.select_related("site"))

    device_health = {"live": 0, "delayed": 0, "offline": 0, "alarm": 0}
    for device in devices:
        device_health[device.freshness.status] = device_health.get(device.freshness.status, 0) + 1
    gateway_health = {"live": 0, "offline": 0, "maintenance": 0}
    for gateway in gateways:
        gateway_health[gateway.freshness.status] = gateway_health.get(gateway.freshness.status, 0) + 1

    active_alerts_qs = (
        Alert.objects.filter(device__team=team, status="active")
        .select_related("device", "rule")
        .order_by("-triggered_at")
    )
    if visible_site_ids is not None:
        active_alerts_qs = active_alerts_qs.filter(device__site_id__in=visible_site_ids)
    active_alerts_count = active_alerts_qs.count()
    active_rules_qs = AlertRule.objects.filter(team=team, is_active=True).select_related("device")
    if visible_site_ids is not None:
        from django.db.models import Q

        active_rules_qs = active_rules_qs.filter(
            Q(site_id__in=visible_site_ids) | Q(device__site_id__in=visible_site_ids)
        ).distinct()
    active_rules = list(active_rules_qs)
    active_rule_count = len(active_rules)
    active_alerts = list(active_alerts_qs[:10])
    recent_alerts_qs = Alert.objects.filter(device__team=team).select_related("device", "rule")
    ticket_qs = MaintenanceTicket.objects.filter(team=team)
    pm_qs = PreventiveSchedule.objects.filter(team=team)
    if visible_site_ids is not None:
        recent_alerts_qs = recent_alerts_qs.filter(device__site_id__in=visible_site_ids)
        ticket_qs = ticket_qs.filter(device__site_id__in=visible_site_ids)
        pm_qs = pm_qs.filter(device__site_id__in=visible_site_ids)
    recent_alerts = list(recent_alerts_qs.order_by("-triggered_at")[:5])
    open_tickets = ticket_qs.filter(status__in=["open", "in_progress", "waiting"]).count()
    overdue_pms = pm_qs.filter(is_active=True, next_due_at__lt=timezone.now()).count()
    active_automations = Automation.objects.filter(team=team, is_active=True).count() if visible_site_ids is None else 0
    total_automations = Automation.objects.filter(team=team).count() if visible_site_ids is None else 0

    classifications = {}
    metric_groups = {}
    for device in devices:
        classification = classify_device(device)
        classifications[device.id] = classification
        metric_groups.setdefault(classification["type"], {"label": classification["label"], "count": 0})["count"] += 1

    top_devices = []
    for device in devices[:12]:
        readings = _latest_readings_for_device(device, limit=2)
        top_devices.append(
            {
                "device": device,
                "classification": classifications[device.id],
                "freshness": device.freshness,
                "gateway_context": device.gateway_context_display,
                "latest_reading": readings[0] if readings else None,
            }
        )

    site_cards = []
    for site in sites:
        site_devices = [device for device in devices if device.site_id == site.id]
        site_gateways = [gateway for gateway in gateways if gateway.site_id == site.id]
        site_cards.append(
            {
                "site": site,
                "devices_count": len(site_devices),
                "gateways_count": len(site_gateways),
                "monitoring_active": any(
                    rule.site_id == site.id
                    or (rule.device_id and rule.device.site_id == site.id)
                    or (rule.site_id is None and rule.device_id is None)
                    for rule in active_rules
                ),
                "attention_count": sum(
                    1 for device in site_devices if device.freshness.status in {"delayed", "offline", "alarm"}
                )
                + sum(1 for gateway in site_gateways if gateway.freshness.status == "offline"),
            }
        )

    metric_group_values = list(metric_groups.values())
    for group in metric_group_values:
        group["percentage"] = round((group["count"] / len(devices)) * 100) if devices else 0

    impact_summary = None
    if include_impact:
        from apps.impact.services import build_team_impact_summary

        impact_summary = build_team_impact_summary(team, site_ids=impact_site_ids)

    return {
        "sites": sites,
        "sites_count": len(sites),
        "gateways_count": len(gateways),
        "devices_count": len(devices),
        "device_health": device_health,
        "gateway_health": gateway_health,
        "devices_live": device_health.get("live", 0),
        "devices_attention": device_health.get("delayed", 0)
        + device_health.get("offline", 0)
        + device_health.get("alarm", 0),
        "gateways_online": gateway_health.get("live", 0),
        "gateways_offline": gateway_health.get("offline", 0),
        "active_alerts_count": active_alerts_count,
        "active_rule_count": active_rule_count,
        "open_tickets": open_tickets,
        "overdue_pms": overdue_pms,
        "active_automations": active_automations,
        "total_automations": total_automations,
        "metric_groups": metric_group_values,
        "has_energy_widgets": "energy" in metric_groups,
        "operations_trend": _build_operations_trend(team, devices),
        "recent_alerts": recent_alerts,
        "top_devices": top_devices,
        "attention_items": _build_attention_items(
            team, devices, gateways, active_alerts, open_tickets, overdue_pms, active_rule_count
        ),
        "site_cards": site_cards,
        "logs": ActivityLog.objects.filter(team=team).order_by("-timestamp")[:15] if visible_site_ids is None else [],
        "impact_summary": impact_summary,
    }


def build_device_dashboard_context(device):
    dashboard = Dashboard.objects.filter(device=device, is_default=True).first()
    widgets = dashboard.widgets.all() if dashboard else []
    registers = _register_items(device, include_writable_display=True)
    readable_registers = [item for item in registers if not item["writable"]]
    writable_registers = [item for item in registers if item["writable"]]
    return {
        "classification": classify_device(device),
        "latest_readings": _latest_readings_for_device(device, limit=4),
        "dashboard": dashboard,
        "widgets": widgets,
        "readable_registers": readable_registers,
        "writable_registers": writable_registers,
        "has_template": bool(_register_items(device)),
    }
