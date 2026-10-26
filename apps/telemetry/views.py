import csv

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from waffle import flag_is_active

from apps.devices.datapoint_maps import effective_register_map
from apps.devices.models import Device, Site
from apps.devices.solution_profiles import get_site_profile
from apps.teams.decorators import login_and_team_required
from apps.utils.timezones import format_site_datetime, site_timezone_metadata

from .models import TelemetryData
from .services import get_latest_telemetry_for_chart, get_latest_telemetry_value

SAMPLE_LIMIT_OPTIONS = (10, 20, 30, 40, 50)
DEFAULT_SAMPLE_LIMIT = 20


def _normalize_sample_limit(raw_limit):
    try:
        requested = int(raw_limit)
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_LIMIT

    if requested <= SAMPLE_LIMIT_OPTIONS[0]:
        return SAMPLE_LIMIT_OPTIONS[0]
    if requested >= SAMPLE_LIMIT_OPTIONS[-1]:
        return SAMPLE_LIMIT_OPTIONS[-1]
    return min(SAMPLE_LIMIT_OPTIONS, key=lambda option: abs(option - requested))


def _telemetry_point_value(point):
    if point.value_numeric is not None:
        return point.value_numeric
    if point.value_bool is not None:
        return point.value_bool
    return point.value_string


def _device_telemetry_columns(device):
    columns = []
    seen = set()

    register_map = effective_register_map(device)
    if register_map:
        for key, config in register_map.items():
            if not isinstance(config, dict) or config.get("writable"):
                continue
            columns.append(
                {
                    "key": key,
                    "label": config.get("label", key.replace("_", " ").title()),
                    "unit": config.get("unit", ""),
                }
            )
            seen.add(key)

    if not columns:
        keys = TelemetryData.objects.filter(device=device).order_by("key").values_list("key", flat=True).distinct()
        for key in keys:
            columns.append(
                {
                    "key": key,
                    "label": key.replace("_", " ").title(),
                    "unit": "",
                }
            )
            seen.add(key)

    return columns, seen


@login_and_team_required
def get_chart_partial(request, team_slug, device_id, key):
    # Security: Ensure device belongs to the team
    device = get_object_or_404(Device, id=device_id, team__slug=team_slug)

    chart_data = get_latest_telemetry_for_chart(device, key)

    context = {
        "chart_data": chart_data,
        "device": device,
    }
    return render(request, "telemetry/partials/chart_partial.html", context)


@login_and_team_required
def get_kpi_partial(request, team_slug, device_id, key):
    device = get_object_or_404(Device, id=device_id, team__slug=team_slug)
    value = get_latest_telemetry_value(device, key)

    units = {"voltage": "V", "active_power": "W", "frequency": "Hz", "solar_generation": "W"}

    context = {
        "value": value,
        "label": key.replace("_", " "),
        "unit": units.get(key, ""),
    }
    return render(request, "telemetry/partials/kpi_card.html", context)


@login_and_team_required
def telemetry_analyzer(request, team_slug, device_id):
    """
    Main view for the historical data analyzer.
    """
    device = get_object_or_404(Device, id=device_id, team__slug=team_slug)

    context = {
        "device": device,
        "team": request.team,
        "active_tab": "devices",
        "page_title": f"Analyze: {device.name}",
        "retention_days": get_retention_limit_days(request.team),
    }
    return render(request, "telemetry/analyzer.html", context)


@login_and_team_required
def device_metrics_api(request, team_slug, device_id):
    """
    JSON endpoint returning available telemetry metrics (keys, labels, units) for a device.
    """
    device = get_object_or_404(Device, id=device_id, team__slug=team_slug)

    metrics = []
    register_map = effective_register_map(device)
    if register_map:
        for key, val in register_map.items():
            if isinstance(val, dict) and not val.get("writable"):
                metrics.append(
                    {"key": key, "label": val.get("label", key.replace("_", " ").title()), "unit": val.get("unit", "")}
                )

    if not metrics:
        # Fallback to distinct keys recorded in TelemetryData
        keys = TelemetryData.objects.filter(device=device).order_by().values_list("key", flat=True).distinct()
        for key in keys:
            metrics.append({"key": key, "label": key.replace("_", " ").title(), "unit": ""})

    return JsonResponse({"metrics": metrics})


@login_and_team_required
def device_telemetry_samples_api(request, team_slug, device_id):
    """
    JSON endpoint returning the latest grouped telemetry samples for a device.

    TelemetryData is stored as one row per key, so this endpoint pivots rows
    with the same timestamp into a table-friendly sample row.
    """
    device = get_object_or_404(Device, id=device_id, team__slug=team_slug)
    limit = _normalize_sample_limit(request.GET.get("limit", DEFAULT_SAMPLE_LIMIT))
    columns, column_keys = _device_telemetry_columns(device)

    sample_qs = TelemetryData.objects.filter(device=device)
    if column_keys:
        sample_qs = sample_qs.filter(key__in=column_keys)

    timestamps = list(sample_qs.order_by("-timestamp").values_list("timestamp", flat=True).distinct()[:limit])

    points = sample_qs.filter(timestamp__in=timestamps).order_by("-timestamp", "key")

    rows_by_timestamp = {
        timestamp: {
            "timestamp": timestamp.isoformat(),
            "timestamp_local": format_site_datetime(timestamp, device.site),
            "values": {},
        }
        for timestamp in timestamps
    }

    for point in points:
        row = rows_by_timestamp.get(point.timestamp)
        if row is not None:
            row["values"][point.key] = _telemetry_point_value(point)

    return JsonResponse(
        {
            "columns": columns,
            "rows": list(rows_by_timestamp.values()),
            "limit": limit,
            "limit_options": list(SAMPLE_LIMIT_OPTIONS),
            **site_timezone_metadata(device.site),
        }
    )


def get_retention_limit_days(team) -> int:
    from apps.subscriptions.enforcement import get_retention_limit_days_for_team

    return get_retention_limit_days_for_team(team)


@login_and_team_required
def device_telemetry_history_api(request, team_slug, device_id):
    """
    JSON endpoint for historical and real-time Chart.js updates.
    """
    from django.utils import timezone

    device = get_object_or_404(Device, id=device_id, team__slug=team_slug)
    key = request.GET.get("key", "active_power")
    try:
        hours = max(1, int(request.GET.get("hours", 24)))
    except (TypeError, ValueError):
        hours = 24

    plan_days = get_retention_limit_days(request.team)
    max_hours = plan_days * 24

    # Plan retention is a customer-visible history window. Rows can remain in
    # TimescaleDB until the global physical retention window expires, which
    # lets an upgrade reveal retained history without making downgrade access
    # linger.
    if hours > max_hours:
        hours = max_hours

    qs = TelemetryData.objects.filter(device=device)
    if key:
        qs = qs.filter(key=key)

    cutoff = timezone.now() - timezone.timedelta(hours=hours)
    qs = qs.filter(timestamp__gte=cutoff).order_by("-timestamp")[:1000]

    # Reverse to keep chronological order
    points = list(reversed(list(qs)))

    labels = []
    labels_local = []
    values = []
    for point in points:
        labels.append(point.timestamp.isoformat())
        labels_local.append(format_site_datetime(point.timestamp, device.site, "%H:%M:%S"))
        values.append(_telemetry_point_value(point))

    return JsonResponse(
        {
            "labels": labels,
            "labels_local": labels_local,
            "values": values,
            "key": key,
            **site_timezone_metadata(device.site),
        }
    )


@login_and_team_required
def export_telemetry_csv(request, team_slug, device_id):
    """
    Exports device telemetry to CSV.
    """
    from django.utils import timezone

    device = get_object_or_404(Device, id=device_id, team__slug=team_slug)
    days = int(request.GET.get("days", 7))

    plan_days = get_retention_limit_days(request.team)
    # CSV export follows the same visible-history rule as charts/API queries;
    # it is not responsible for physical database deletion.
    if days > plan_days:
        days = plan_days

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{device.name}_telemetry.csv"'

    writer = csv.writer(response)
    writer.writerow(["Timestamp", "Metric", "Key", "Value", "Unit"])
    columns, _ = _device_telemetry_columns(device)
    metric_meta = {
        column["key"]: {
            "label": column.get("label") or column["key"].replace("_", " ").title(),
            "unit": column.get("unit", ""),
        }
        for column in columns
    }

    cutoff = timezone.now() - timezone.timedelta(days=days)
    qs = TelemetryData.objects.filter(device=device, timestamp__gte=cutoff).order_by("-timestamp")
    for point in qs[:5000]:  # Limit export for safety
        val = point.value_numeric if point.value_numeric is not None else (point.value_string or point.value_bool)
        meta = metric_meta.get(point.key, {"label": point.key.replace("_", " ").title(), "unit": ""})
        writer.writerow(
            [
                format_site_datetime(point.timestamp, device.site),
                meta["label"],
                point.key,
                val,
                meta["unit"],
            ]
        )

    return response


def _site_profile_report_rows(site, days):
    from django.utils import timezone

    from apps.alerts.models import Alert
    from apps.devices.freshness import compact_timesince, device_freshness_thresholds
    from apps.maintenance.models import MaintenanceTicket, PreventiveSchedule

    cutoff = timezone.now() - timezone.timedelta(days=days)
    profile = get_site_profile(site)
    devices = list(site.devices.select_related("template", "gateway"))
    rows = []

    for device in devices:
        delayed_seconds, offline_seconds = device_freshness_thresholds(device)
        register_map = effective_register_map(device)
        for key in profile.key_priority:
            if register_map and key not in register_map:
                continue
            latest = (
                TelemetryData.objects.filter(device=device, key=key, timestamp__gte=cutoff)
                .order_by("-timestamp")
                .first()
            )
            if not latest:
                continue
            value = _telemetry_point_value(latest)
            meta = register_map.get(key, {}) if isinstance(register_map, dict) else {}
            if isinstance(value, bool):
                display_value = ("Open" if value else "Closed") if key == "door_open" else ("On" if value else "Off")
            elif isinstance(value, int | float):
                display_value = f"{value:.2f}".rstrip("0").rstrip(".")
            else:
                display_value = value if value is not None else "—"
            age_seconds = max(0, int((timezone.now() - latest.timestamp).total_seconds()))
            if age_seconds >= offline_seconds:
                reading_status = "Offline"
            elif age_seconds >= delayed_seconds:
                reading_status = "Delayed"
            else:
                reading_status = "Live"
            rows.append(
                {
                    "device": device,
                    "metric": meta.get("label", key.replace("_", " ").title()),
                    "key": key,
                    "value": display_value,
                    "unit": {"degC": "°C", "degF": "°F"}.get(meta.get("unit"), meta.get("unit", "")),
                    "reading_status": reading_status,
                    "reading_age": compact_timesince(latest.timestamp),
                    "timestamp": latest.timestamp,
                    "timestamp_display": format_site_datetime(latest.timestamp, site),
                }
            )

    return {
        "profile": profile,
        "devices": devices,
        "rows": rows,
        "active_alerts": Alert.objects.filter(device__site=site, status="active").select_related("device", "rule"),
        "open_tickets": MaintenanceTicket.objects.filter(
            device__site=site, status__in=["open", "in_progress", "waiting"]
        ),
        "overdue_pms": PreventiveSchedule.objects.filter(
            device__site=site, is_active=True, next_due_at__lt=timezone.now()
        ),
        "days": days,
    }


@login_and_team_required
def site_profile_report(request, team_slug, site_id):
    site = get_object_or_404(Site, id=site_id, team=request.team)
    if flag_is_active(request, "business_impact_roi"):
        return redirect("web_team:impact:site_detail", team_slug=team_slug, site_id=site_id)
    days = int(request.GET.get("days", 7))
    context = {
        "site": site,
        "team": request.team,
        "active_tab": "sites",
        **_site_profile_report_rows(site, days),
    }
    return render(request, "telemetry/site_profile_report.html", context)


@login_and_team_required
def export_site_profile_report_csv(request, team_slug, site_id):
    site = get_object_or_404(Site, id=site_id, team__slug=team_slug)
    days = int(request.GET.get("days", 7))
    report = _site_profile_report_rows(site, days)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{site.name}_{report["profile"].key}_report.csv"'
    writer = csv.writer(response)
    writer.writerow(["Site", "Profile", "Device", "Metric", "Key", "Value", "Unit", "Timestamp", "Reading Status"])
    for row in report["rows"]:
        writer.writerow(
            [
                site.name,
                report["profile"].name,
                row["device"].name,
                row["metric"],
                row["key"],
                row["value"],
                row["unit"],
                row["timestamp_display"],
                row["reading_status"],
            ]
        )
    return response
