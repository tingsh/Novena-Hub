import ipaddress
import json
import re
import uuid
from datetime import timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from waffle import flag_is_active

from apps.alerts.models import AlertRule
from apps.devices.models import (
    DeploymentSetupItem,
    DeploymentSetupRun,
    Device,
    DeviceTemplate,
    EquipmentTemplateRequest,
    Gateway,
    RemoteCommand,
    Site,
)
from apps.devices.services import build_commissioning_context, visible_templates_for_team
from apps.devices.solution_profiles import (
    apply_solution_profile_presets,
    get_profile,
    get_site_profile,
    profile_for_request,
    rank_templates_for_profile,
    recommended_alerts_for_device,
)
from apps.teams.decorators import require_permission

ONBOARDING_STEPS = [
    {"num": 1, "label": "Location"},
    {"num": 2, "label": "Gateway"},
    {"num": 3, "label": "Equipment"},
    {"num": 4, "label": "Verify"},
    {"num": 5, "label": "Go live"},
]

SETUP_GOAL_CHOICES = (
    ("equipment_health", "Monitor equipment health"),
    ("energy_usage", "Track energy usage"),
    ("abnormal_reading_alerts", "Get alerts for abnormal readings"),
    ("reports_audit_trail", "Prepare reports / audit trail"),
    ("maintenance_reminders", "Set up maintenance reminders"),
    ("not_sure", "Not sure yet"),
    ("other", "Other"),
)

OPERATING_HOURS_CHOICES = (
    ("always_on", "24/7"),
    ("custom", "Custom"),
    ("irregular_schedule", "Irregular schedule"),
    ("not_sure", "Not sure yet"),
)


def _bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _bounded_float(value, default, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _candidate_connection_from_post(post, candidate, index):
    """Apply customer-edited connection values to one discovered candidate."""
    from apps.devices.deployment_setup import connection_from_candidate

    candidate = dict(candidate)
    connection = connection_from_candidate(candidate)
    slave_id = int(post.get(f"slave_id_{index}", connection.get("slave_id") or 1))
    if not 1 <= slave_id <= 247:
        raise ValueError("Equipment unit ID must be between 1 and 247")
    connection["slave_id"] = slave_id
    protocol = candidate.get("connection") or candidate.get("protocol")
    if protocol == "modbus_tcp":
        host = post.get(f"host_{index}", connection.get("host", "")).strip()
        try:
            parsed_host = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Enter a valid equipment IP address") from exc
        if parsed_host.is_unspecified or parsed_host.is_loopback or parsed_host.is_multicast:
            raise ValueError("Enter a safe, reachable equipment IP address")
        port = int(post.get(f"port_{index}", connection.get("port", 502)))
        if not 1 <= port <= 65535:
            raise ValueError("Modbus TCP port must be between 1 and 65535")
        connection.update({"host": str(parsed_host), "port": port})
        candidate.update(
            {
                "host": str(parsed_host),
                "port": port,
                "slave_id": slave_id,
                "interface": f"{parsed_host}:{port}",
            }
        )
    else:
        candidate["slave_id"] = slave_id
    return connection, candidate


def _candidate_for_index(discovered_devices, run, index):
    """Return current discovery evidence, falling back to a durable saved draft."""
    if 0 <= index < len(discovered_devices):
        return dict(discovered_devices[index])
    draft_item = run.items.filter(discovery_index=index, device__isnull=True).first()
    if draft_item and draft_item.candidate_data:
        return dict(draft_item.candidate_data)
    raise IndexError("Equipment is no longer available in this setup run")


def _reattach_running_discovery(run):
    """Keep Guided Setup aligned with a Gateway scan that is already running."""
    discovery = run.gateway.discovery_data or {}
    scan_id = str(discovery.get("scan_id") or "")
    if discovery.get("status") != "running" or not scan_id:
        return run
    summary = dict(run.summary or {})
    discovery_meta = dict(summary.get("discovery") or {})
    if discovery_meta.get("active_scan_id") == scan_id:
        return run
    discovery_meta.update(
        {
            "active_scan_id": scan_id,
            "reattached_at": timezone.now().isoformat(),
        }
    )
    discovery_meta.pop("command_id", None)
    summary["discovery"] = discovery_meta
    run.summary = summary
    run.state = DeploymentSetupRun.State.DISCOVERING
    run.current_step = "equipment"
    run.save(update_fields=["summary", "state", "current_step", "updated_at"])
    return run


def _has_active_discovery_scan(run):
    """Return true only when the Gateway scan is actually active, not merely held in a UX state."""
    discovery_meta = (run.summary or {}).get("discovery") or {}
    scan_id = str(discovery_meta.get("active_scan_id") or "")
    if not scan_id:
        return False

    discovery = run.gateway.discovery_data or {}
    if str(discovery.get("scan_id") or "") == scan_id:
        return discovery.get("status") == "running"

    command_id = discovery_meta.get("command_id")
    if command_id:
        command = RemoteCommand.objects.filter(pk=command_id, gateway=run.gateway).first()
        if command and command.status in {
            RemoteCommand.Status.POLICY_DENIED,
            RemoteCommand.Status.REJECTED,
            RemoteCommand.Status.FAILED,
            RemoteCommand.Status.EXPIRED,
            RemoteCommand.Status.TIMED_OUT,
            RemoteCommand.Status.OUTCOME_UNKNOWN,
        }:
            return False
        return bool(command)

    started_at_raw = discovery_meta.get("started_at")
    if started_at_raw:
        try:
            started_at = timezone.datetime.fromisoformat(str(started_at_raw).replace("Z", "+00:00"))
            if timezone.is_naive(started_at):
                started_at = timezone.make_aware(started_at, dt_timezone.utc)
            started_at = started_at.astimezone(dt_timezone.utc)
        except (TypeError, ValueError):
            return False
        return timezone.now() - started_at < timedelta(seconds=30)

    return False


def _is_valid_timezone_name(value):
    if not value:
        return False
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True


def _resolve_onboarding_timezone(request, site=None):
    candidates = (
        request.POST.get("timezone", "").strip(),
        getattr(site, "timezone", ""),
        getattr(request.user, "timezone", ""),
        getattr(settings, "TIME_ZONE", ""),
        "UTC",
    )
    for candidate in candidates:
        if _is_valid_timezone_name(candidate):
            return candidate
    return "UTC"


def _valid_choice_value(value, choices):
    if value in {choice[0] for choice in choices}:
        return value
    return ""


def _valid_choice_values(values, choices):
    allowed_values = {choice[0] for choice in choices}
    valid_values = []
    for value in values:
        if value in allowed_values and value not in valid_values:
            valid_values.append(value)
    return valid_values


def _valid_time_value(value, default):
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        return value
    return default


def _site_onboarding_context(site):
    metadata = getattr(site, "metadata", None) or {}
    onboarding = metadata.get("onboarding", {})
    if not isinstance(onboarding, dict):
        return {"setup_goals": []}
    onboarding = dict(onboarding)
    setup_goals = onboarding.get("setup_goals")
    if isinstance(setup_goals, list):
        onboarding["setup_goals"] = setup_goals
    elif onboarding.get("setup_goal"):
        onboarding["setup_goals"] = [onboarding["setup_goal"]]
    else:
        onboarding["setup_goals"] = []
    return onboarding


def _update_site_onboarding_context(site, request):
    onboarding = dict(_site_onboarding_context(site))
    setup_goals = _valid_choice_values(request.POST.getlist("setup_goals"), SETUP_GOAL_CHOICES)
    operating_hours = _valid_choice_value(request.POST.get("operating_hours", ""), OPERATING_HOURS_CHOICES)
    posted_values = {
        "operating_hours": operating_hours,
    }
    onboarding.pop("setup_goal", None)
    if setup_goals:
        onboarding["setup_goals"] = setup_goals
    else:
        onboarding.pop("setup_goals", None)

    setup_goal_other = request.POST.get("setup_goal_other", "").strip()[:200]
    if "other" in setup_goals and setup_goal_other:
        onboarding["setup_goal_other"] = setup_goal_other
    else:
        onboarding.pop("setup_goal_other", None)

    for key, value in posted_values.items():
        if value:
            onboarding[key] = value
        else:
            onboarding.pop(key, None)

    if operating_hours == "custom":
        onboarding["operating_hours_custom"] = {
            "start": _valid_time_value(request.POST.get("custom_operating_start"), "08:00"),
            "end": _valid_time_value(request.POST.get("custom_operating_end"), "18:00"),
        }
    else:
        onboarding.pop("operating_hours_custom", None)

    metadata = dict(site.metadata or {})
    if onboarding:
        metadata["onboarding"] = onboarding
    else:
        metadata.pop("onboarding", None)
    site.metadata = metadata


@require_permission("manage_devices")
def onboarding_start(request, team_slug):
    if Site.objects.filter(team=request.team).exists():
        return redirect("web_team:onboarding:setup_start", team_slug=team_slug)
    return render(request, "onboarding/welcome.html")


@require_permission("manage_devices")
def step_profile(request, team_slug):
    from apps.devices.solution_profiles import PROFILES

    selected = request.session.get("solution_profile", "general_iot")
    if request.method == "POST":
        selected = request.POST.get("solution_profile", "general_iot")
        request.session["solution_profile"] = get_profile(selected).key
        return redirect("web_team:onboarding:step_1_site", team_slug=team_slug)

    context = {
        "steps": ONBOARDING_STEPS,
        "current_step": 1,
        "profiles": PROFILES.values(),
        "selected_profile": selected,
    }
    return render(request, "onboarding/step_profile.html", context)


@require_permission("manage_devices")
def step_1_site(request, team_slug):
    if not request.session.get("solution_profile"):
        request.session["solution_profile"] = "general_iot"

    site_id = request.session.get("onboarding_site_id")
    site = Site.objects.filter(id=site_id, team=request.team).first() if site_id else None
    profile = profile_for_request(request)

    if request.method == "POST":
        name = request.POST.get("name")
        address = request.POST.get("address", "")
        timezone = _resolve_onboarding_timezone(request, site)
        solution_profile = request.POST.get("solution_profile") or request.session.get(
            "solution_profile", "general_iot"
        )
        if name:
            if site:
                site.name = name
                site.address = address
                site.timezone = timezone
                site.solution_profile = get_profile(solution_profile).key
                _update_site_onboarding_context(site, request)
                site.save()
            else:
                site = Site.objects.create(
                    team=request.team,
                    name=name,
                    address=address,
                    timezone=timezone,
                    solution_profile=get_profile(solution_profile).key,
                )
                _update_site_onboarding_context(site, request)
                site.save(update_fields=["metadata", "updated_at"])
            request.session["onboarding_site_id"] = site.id
            request.session["solution_profile"] = site.solution_profile
            if flag_is_active(request, "business_impact_roi"):
                from apps.impact.services import (
                    create_assumption_revision,
                    ensure_business_profile,
                    ensure_site_profile,
                )

                impact_profile = ensure_site_profile(site)
                currency = request.POST.get("impact_currency", "SGD").strip().upper() or "SGD"
                if len(currency) != 3 or not currency.isalpha():
                    currency = "SGD"
                business_profile = ensure_business_profile(request.team)
                business_profile.currency = currency
                business_profile.full_clean()
                business_profile.save(update_fields=["currency", "updated_at"])
                operating_start = request.POST.get("operating_start", "08:00")
                operating_end = request.POST.get("operating_end", "18:00")
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", operating_start):
                    operating_start = "08:00"
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", operating_end):
                    operating_end = "18:00"
                weekdays = {"monday", "tuesday", "wednesday", "thursday", "friday"}
                impact_profile.operating_schedule = {
                    day: ([[operating_start, operating_end]] if day in weekdays else [])
                    for day in (
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    )
                }
                impact_profile.save(update_fields=["operating_schedule", "updated_at"])
                assumption_inputs = {
                    "currency": currency,
                    "tariff_per_kwh": request.POST.get("tariff_per_kwh") or None,
                    "downtime_cost_per_hour": request.POST.get("downtime_cost_per_hour") or None,
                    "labor_cost_per_hour": request.POST.get("labor_cost_per_hour") or None,
                    "cold_min_temperature": request.POST.get("cold_min_temperature") or None,
                    "cold_max_temperature": request.POST.get("cold_max_temperature") or None,
                }
                if any(value is not None for key, value in assumption_inputs.items() if key != "currency"):
                    try:
                        create_assumption_revision(
                            impact_profile,
                            assumption_inputs,
                            user=request.user,
                            change_note="Captured during site onboarding",
                        )
                    except ValidationError:
                        messages.warning(
                            request,
                            "The site was saved, but some optional business inputs were invalid. "
                            "You can correct them later in Business Impact settings.",
                        )
            if request.session.get("setup_mode"):
                return redirect("web_team:onboarding:step_connectivity", team_slug=team_slug)
            return redirect("web_team:onboarding:step_2_gateway", team_slug=team_slug)

    context = {
        "steps": ONBOARDING_STEPS,
        "current_step": 1,
        "site": site,
        "profile": get_site_profile(site) if site else profile,
        "setup_goal_choices": SETUP_GOAL_CHOICES,
        "operating_hours_choices": OPERATING_HOURS_CHOICES,
        "onboarding_context": _site_onboarding_context(site),
    }
    return render(request, "onboarding/step_1_site.html", context)


@require_permission("manage_devices")
def step_2_gateway(request, team_slug):
    from apps.devices.services import GatewayClaimError, claim_gateway_for_team

    site_id = request.session.get("onboarding_site_id")
    if not site_id:
        return redirect("web_team:onboarding:step_1_site", team_slug=team_slug)
    site = get_object_or_404(Site, id=site_id, team=request.team)

    gateway_id = request.session.get("onboarding_gateway_id")
    gateway = Gateway.objects.filter(id=gateway_id, team=request.team).first() if gateway_id else None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        sn = request.POST.get("serial_number", "").strip().upper()
        claim_code = request.POST.get("claim_code", "").strip().upper()

        error = None
        if not name or not sn:
            error = "Gateway name and serial number are required."
        elif len(sn) < 3 or len(sn) > 50:
            error = "Serial number must be between 3 and 50 characters."
        elif not claim_code:
            error = "Claim code is required. Check the sticker on the bottom of your gateway."
        else:
            try:
                gateway = claim_gateway_for_team(request.team, site, name, sn, claim_code)
            except GatewayClaimError as e:
                error = str(e)

        if error:
            context = {
                "steps": ONBOARDING_STEPS,
                "current_step": 2,
                "site": site,
                "gateway": gateway,
                "profile": get_site_profile(site),
                "error": error,
            }
            return render(request, "onboarding/step_2_gateway.html", context)

        request.session["onboarding_gateway_id"] = gateway.id
        return redirect("web_team:onboarding:step_2b_wait", team_slug=team_slug)

    context = {
        "steps": ONBOARDING_STEPS,
        "current_step": 2,
        "site": site,
        "gateway": gateway,
        "profile": get_site_profile(site),
    }
    return render(request, "onboarding/step_2_gateway.html", context)


@require_permission("manage_devices")
def step_2b_wait(request, team_slug):
    """Intermediate step: wait for the gateway to come online after claiming."""
    gateway_id = request.session.get("onboarding_gateway_id")
    if not gateway_id:
        return redirect("web_team:onboarding:step_2_gateway", team_slug=team_slug)
    gateway = get_object_or_404(Gateway, id=gateway_id, team=request.team)

    if gateway.status == "online" and gateway.lifecycle_status == "claimed":
        gateway.lifecycle_status = "online"
        gateway.save(update_fields=["lifecycle_status"])

    from apps.devices.deployment_setup import (
        append_setup_event,
        gateway_readiness,
        get_or_create_setup_run,
        sync_setup_run,
    )
    from apps.devices.gateway_config_delivery import gateway_supports_guided_setup

    run = get_or_create_setup_run(team=request.team, gateway=gateway, initiated_by=request.user)
    run = sync_setup_run(run)
    readiness = gateway_readiness(gateway)
    cached_status = (run.readiness or {}).get("status")
    should_refresh_readiness = (
        not run.readiness
        or not run.events.filter(event_type="gateway_preflight_completed").exists()
        or cached_status != "ready"
        or readiness.get("status") == "ready"
    )
    if should_refresh_readiness:
        run.readiness = readiness
        run.save(update_fields=["readiness", "updated_at"])
    else:
        readiness = run.readiness
    if (
        gateway.status == "online"
        and gateway_supports_guided_setup(gateway)
        and not run.events.filter(event_type="gateway_preflight_started").exists()
    ):
        try:
            from apps.devices.remote_control import request_remote_command

            command = request_remote_command(
                gateway=gateway,
                operation="deployment_preflight",
                requested_by=request.user,
                reason="Guided Setup Gateway readiness",
                ttl_seconds=120,
            )
            append_setup_event(
                run,
                "gateway_preflight_started",
                "Gateway readiness checks started.",
                actor=request.user,
                evidence={"command_id": str(command.pk)},
            )
        except Exception as exc:
            append_setup_event(
                run,
                "gateway_preflight_unavailable",
                "Gateway readiness checks could not start.",
                actor=request.user,
                evidence={"error": str(exc)},
            )

    context = {
        "steps": ONBOARDING_STEPS,
        "current_step": 2,
        "gateway": gateway,
        "profile": get_site_profile(gateway.site),
        "commissioning": build_commissioning_context(request.team, gateway=gateway, session=request.session),
        "setup_run": run,
        "readiness": readiness,
    }
    return render(request, "onboarding/step_2b_wait.html", context)


@require_permission("view_devices")
def gateway_status_poll(request, team_slug):
    """HTMX endpoint: returns a small HTML fragment with current gateway status."""
    gateway_id = request.session.get("onboarding_gateway_id")
    if not gateway_id:
        return render(request, "onboarding/partials/gateway_status_badge.html", {"status": "unknown"})
    gateway = Gateway.objects.filter(id=gateway_id, team=request.team).first()
    # Resolve the badge from heartbeat freshness, not only the last persisted
    # status. This prevents the setup screen from saying "online" during the
    # interval before the background timeout task marks a stale gateway offline.
    status = "online" if gateway and gateway.freshness.status == "live" else ("offline" if gateway else "unknown")
    return render(
        request,
        "onboarding/partials/gateway_status_badge.html",
        {
            "status": status,
            "gateway": gateway,
            "commissioning": build_commissioning_context(request.team, gateway=gateway, session=request.session),
        },
    )


@require_permission("manage_devices")
def step_3_discover(request, team_slug):
    """Automatic-first equipment setup with guided manual fallback."""
    from apps.devices.config_generator import (
        generate_and_push_config,
        human_config_preview,
        normalized_datapoints,
    )
    from apps.devices.deployment_setup import (
        append_setup_event,
        create_or_update_candidate_item,
        discovery_scan_state,
        get_or_create_setup_run,
        start_validation,
        sync_setup_run,
    )
    from apps.devices.gateway_config_delivery import gateway_supports_guided_setup

    gateway_id = request.session.get("onboarding_gateway_id")
    if not gateway_id:
        return redirect("web_team:onboarding:step_2_gateway", team_slug=team_slug)
    gateway = get_object_or_404(Gateway, id=gateway_id, team=request.team)
    run = get_or_create_setup_run(team=request.team, gateway=gateway, initiated_by=request.user)
    run = _reattach_running_discovery(run)
    run = sync_setup_run(run)
    guided_capable = gateway_supports_guided_setup(gateway)

    discovery_data = gateway.discovery_data or {}
    discovered_devices = discovery_data.get("devices", [])
    profile = get_site_profile(gateway.site)
    templates = rank_templates_for_profile(visible_templates_for_team(request.team), profile)

    if request.method == "POST":
        action = request.POST.get("action", "validate_selected")
        for candidate_action in (
            "save_candidate_draft",
            "start_custom_template",
            "request_candidate_template",
        ):
            candidate_index = request.POST.get(candidate_action)
            if candidate_index is not None:
                action = f"{candidate_action}:{candidate_index}"
                break
        if action in {"start_discovery", "start_target_discovery"}:
            if not gateway_supports_guided_setup(gateway):
                messages.warning(
                    request,
                    "This Gateway cannot run the safe Guided Setup scan yet. "
                    "Update its software or use the manual option.",
                )
            else:
                run = _reattach_running_discovery(run)
                if _has_active_discovery_scan(run):
                    messages.info(request, "An equipment scan is already running. We’ll keep checking for results.")
                    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
                targeted = action == "start_target_discovery"
                if targeted:
                    target_host = request.POST.get("target_host", "").strip()
                    try:
                        parsed_host = ipaddress.ip_address(target_host)
                    except ValueError:
                        messages.error(request, "Enter a valid equipment IPv4 address.")
                        return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
                    if (
                        parsed_host.version != 4
                        or parsed_host.is_unspecified
                        or parsed_host.is_loopback
                        or parsed_host.is_multicast
                        or parsed_host.is_reserved
                    ):
                        messages.error(request, "Enter a safe, reachable equipment IPv4 address.")
                        return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
                    try:
                        target_port = int(request.POST.get("target_port", "502"))
                    except (TypeError, ValueError):
                        target_port = 0
                    if not 1 <= target_port <= 65535:
                        messages.error(request, "Enter a Modbus TCP port between 1 and 65535.")
                        return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
                    target_host = str(parsed_host)
                scan_id = str(uuid.uuid4())
                started_at = timezone.now()
                summary = dict(run.summary or {})
                summary["discovery"] = {
                    "active_scan_id": scan_id,
                    "started_at": started_at.isoformat(),
                    "visible_until": (started_at + timedelta(seconds=5)).isoformat(),
                    "mode": "approved_target" if targeted else "attached_interfaces",
                    "scope_label": (
                        f"Checking approved endpoint {target_host}:{target_port}"
                        if targeted
                        else "Scanning wired Ethernet and Modbus RTU only"
                    ),
                }
                run.summary = summary
                run.state = DeploymentSetupRun.State.DISCOVERING
                run.current_step = "equipment"
                run.save(update_fields=["summary", "state", "current_step", "updated_at"])
                try:
                    from apps.devices.remote_control import request_remote_command

                    command_params = (
                        {
                            "scan_id": scan_id,
                            "scope": "approved_targets",
                            "tcp_hosts": [{"host": target_host, "port": target_port}],
                            "serial_ports": [],
                        }
                        if targeted
                        else {"scan_id": scan_id, "scope": "attached_interfaces"}
                    )
                    command = request_remote_command(
                        gateway=gateway,
                        operation="deployment_discover",
                        requested_by=request.user,
                        params=command_params,
                        reason=(
                            "Customer-approved specific equipment endpoint discovery"
                            if targeted
                            else "Customer-approved equipment discovery"
                        ),
                        ttl_seconds=300,
                    )
                    summary = dict(run.summary or {})
                    discovery_meta = dict(summary.get("discovery") or {})
                    discovery_meta["command_id"] = str(command.pk)
                    discovery_meta.setdefault("active_scan_id", scan_id)
                    discovery_meta.setdefault("started_at", started_at.isoformat())
                    discovery_meta.setdefault("visible_until", (started_at + timedelta(seconds=5)).isoformat())
                    summary["discovery"] = {
                        **discovery_meta,
                    }
                    run.summary = summary
                    run.save(update_fields=["summary", "updated_at"])
                    append_setup_event(
                        run,
                        "discovery_started",
                        (
                            f"Approved endpoint discovery started for {target_host}:{target_port}."
                            if targeted
                            else "Equipment discovery started."
                        ),
                        actor=request.user,
                        evidence={
                            "command_id": str(command.pk),
                            "scan_id": scan_id,
                            "scope": command_params["scope"],
                            **({"target": f"{target_host}:{target_port}"} if targeted else {}),
                        },
                    )
                    gateway.lifecycle_status = "commissioning"
                    gateway.save(update_fields=["lifecycle_status"])
                except Exception as exc:
                    if "already running" in str(exc).lower():
                        _reattach_running_discovery(run)
                        messages.info(
                            request,
                            "The Gateway is already scanning. We’ll keep checking for results automatically.",
                        )
                    else:
                        messages.error(request, f"Equipment discovery could not start: {exc}")
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        draft_action, separator, raw_draft_index = action.partition(":")
        if separator and draft_action in {
            "save_candidate_draft",
            "start_custom_template",
            "request_candidate_template",
        }:
            try:
                index = int(raw_draft_index)
                candidate = _candidate_for_index(discovered_devices, run, index)
                connection, candidate = _candidate_connection_from_post(request.POST, candidate, index)
            except (IndexError, TypeError, ValueError) as exc:
                messages.error(request, f"This equipment draft could not be saved: {exc}")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

            template = None
            template_id = request.POST.get(f"template_{index}", "").strip()
            if template_id:
                template = visible_templates_for_team(request.team).filter(pk=template_id).first()
                if not template:
                    messages.error(request, "Choose a template available to your organisation.")
                    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

            candidate["customer_name"] = (
                request.POST.get(f"name_{index}", "").strip()
                or candidate.get("customer_name")
                or candidate.get("signature")
                or "Equipment"
            )
            item = create_or_update_candidate_item(run=run, index=index, candidate=candidate)
            item.candidate_data = candidate
            item.connection = connection
            item.selected_template = template
            item.state = (
                DeploymentSetupItem.State.TEMPLATE_SELECTED
                if template
                else DeploymentSetupItem.State.DISCOVERED
            )
            item.save(
                update_fields=[
                    "candidate_data",
                    "connection",
                    "selected_template",
                    "state",
                    "updated_at",
                ]
            )
            append_setup_event(
                run,
                "candidate_draft_saved",
                "Equipment review saved as a draft.",
                item=item,
                actor=request.user,
                evidence={"discovery_index": index, "template_id": template.pk if template else None},
            )

            if draft_action == "start_custom_template":
                return redirect(
                    f"{reverse('web_team:onboarding:step_3_discover', args=[team_slug])}"
                    f"?candidate={index}&workflow=custom#custom-template"
                )
            if draft_action == "request_candidate_template":
                return redirect(
                    f"{reverse('web_team:onboarding:step_3_discover', args=[team_slug])}"
                    f"?candidate={index}&workflow=request#template-request"
                )
            messages.success(request, "Draft saved. You can return and choose a template later.")
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        if action == "cancel_discovery":
            try:
                from apps.devices.remote_control import request_remote_command

                scan_id = str(((run.summary or {}).get("discovery") or {}).get("active_scan_id") or "")
                if not scan_id:
                    raise ValueError("No equipment scan is active")
                command = request_remote_command(
                    gateway=gateway,
                    operation="deployment_discover",
                    requested_by=request.user,
                    params={"scan_id": scan_id, "cancel": True},
                    reason="Customer cancelled equipment discovery",
                    ttl_seconds=60,
                )
                append_setup_event(
                    run,
                    "discovery_cancel_requested",
                    "Equipment discovery cancellation was requested.",
                    actor=request.user,
                    evidence={"command_id": str(command.pk), "scan_id": scan_id},
                )
                messages.info(request, "Cancelling equipment discovery…")
            except Exception as exc:
                messages.error(request, f"Discovery cancellation could not be sent: {exc}")
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        if action == "validate_selected":
            selected = request.POST.getlist("device_index")
            if not selected:
                messages.warning(request, "Select at least one equipment candidate to validate.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            for raw_index in selected:
                try:
                    index = int(raw_index)
                    candidate = _candidate_for_index(discovered_devices, run, index)
                    template = get_object_or_404(
                        visible_templates_for_team(request.team),
                        pk=request.POST.get(f"template_{index}"),
                    )
                    if not guided_capable and not template.is_verified:
                        messages.error(
                            request,
                            "Compatibility mode only permits Novena-verified templates. "
                            "Update the Gateway to validate private or AI draft templates.",
                        )
                        continue
                    connection, candidate = _candidate_connection_from_post(request.POST, candidate, index)
                    item = create_or_update_candidate_item(run=run, index=index, candidate=candidate)
                    if not item.device:
                        from apps.subscriptions.enforcement import can_add_device, get_device_limit_for_team

                        if not can_add_device(request.team):
                            limit = get_device_limit_for_team(request.team)
                            messages.error(
                                request,
                                f"Your current plan supports up to {limit} equipment "
                                f"item{'s' if limit != 1 else ''}. Upgrade your plan or remove unused equipment first.",
                            )
                            break
                    device_name = (
                        request.POST.get(f"name_{index}", "").strip()
                        or candidate.get("signature")
                        or template.name
                    )
                    device_port = str(candidate.get("interface") or candidate.get("port") or "")
                    if item.device:
                        device = item.device
                        device.port = device_port
                        device.name = device_name
                        device.template = template
                        device.device_type = template.device_type
                        device.protocol = template.protocol
                        device.connection_config = connection
                        device.discovery_meta = candidate
                        metadata = dict(device.metadata or {})
                        metadata["guided_setup_validation"] = "pending"
                        device.metadata = metadata
                        device.save()
                    else:
                        device = Device.objects.create(
                            team=request.team,
                            gateway=gateway,
                            site=gateway.site,
                            port=device_port,
                            name=device_name,
                            template=template,
                            device_type=template.device_type,
                            protocol=template.protocol,
                            connection_config=connection,
                            discovery_meta=candidate,
                            metadata={"guided_setup_validation": "pending"},
                        )
                    item.device = device
                    item.connection = connection
                    item.save(update_fields=["device", "connection", "updated_at"])
                    if template.mapping_strategy == "site_defined":
                        from apps.devices.datapoint_maps import ensure_device_datapoint_map

                        mapping = ensure_device_datapoint_map(device)
                        item.selected_template = template
                        item.datapoints = mapping.datapoints
                        item.state = DeploymentSetupItem.State.TEMPLATE_SELECTED
                        item.save(update_fields=["selected_template", "datapoints", "state", "updated_at"])
                        request.session["onboarding_device_id"] = device.pk
                        messages.info(
                            request,
                            "This programmable device needs a site-specific signal map before deployment.",
                        )
                        return redirect(
                            "web_team:devices:device_datapoint_mapping",
                            team_slug=team_slug,
                            pk=device.pk,
                        )
                    if guided_capable:
                        start_validation(item=item, template=template, requested_by=request.user)
                    else:
                        item.selected_template = template
                        item.datapoints = normalized_datapoints(template)
                        item.trust_level = DeploymentSetupItem.Trust.NOVENA_VERIFIED
                        item.state = DeploymentSetupItem.State.VALIDATED
                        item.validation_result = {
                            "status": "compatibility",
                            "message": (
                                "The Novena-verified template matches saved discovery evidence. "
                                "Secure live validation requires a Gateway update."
                            ),
                        }
                        item.save()
                        append_setup_event(
                            run,
                            "legacy_template_confirmed",
                            "A Novena-verified template was confirmed from saved discovery evidence.",
                            item=item,
                            actor=request.user,
                        )
                    request.session["onboarding_device_id"] = device.pk
                except (IndexError, TypeError, ValueError) as exc:
                    messages.error(request, f"One equipment candidate could not be prepared: {exc}")
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        if action == "manual":
            if not guided_capable:
                messages.warning(
                    request,
                    "Guided manual validation requires a Gateway software/key update. "
                    "Use a Novena-verified template from saved discovery results for now.",
                )
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            protocol = request.POST.get("manual_protocol")
            if protocol not in {"modbus_tcp", "modbus_rtu"}:
                messages.error(request, "Choose Modbus TCP or Modbus RTU.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            register_map = {}
            for index in range(1, 11):
                key = re.sub(r"[^a-z0-9_]+", "_", request.POST.get(f"point_key_{index}", "").strip().lower())
                address = request.POST.get(f"point_address_{index}", "").strip()
                if not key or not address:
                    continue
                register_address = _bounded_int(address, None, 0, 65535)
                if register_address is None:
                    continue
                function_code = _bounded_int(request.POST.get(f"point_function_{index}", 3), 3, 1, 4)
                if function_code not in {1, 2, 3, 4}:
                    function_code = 3
                data_type = request.POST.get(f"point_type_{index}", "uint16")
                if data_type not in {"uint16", "int16", "float32", "int32"}:
                    data_type = "uint16"
                default_count = 2 if data_type in {"float32", "int32"} else 1
                register_map[key] = {
                    "label": request.POST.get(f"point_label_{index}", "").strip() or key.replace("_", " ").title(),
                    "address": register_address,
                    "functionCode": function_code,
                    "type": data_type,
                    "objectsCount": _bounded_int(
                        request.POST.get(f"point_count_{index}", default_count),
                        default_count,
                        1,
                        4,
                    ),
                    "scale": _bounded_float(request.POST.get(f"point_scale_{index}", 1), 1, -100000, 100000),
                    "unit": request.POST.get(f"point_unit_{index}", "").strip(),
                }
            if not register_map:
                messages.error(request, "Add at least one signal to validate.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            byte_order = request.POST.get("manual_byte_order", "BIG")
            word_order = request.POST.get("manual_word_order", "BIG")
            connection = {
                "slave_id": _bounded_int(request.POST.get("manual_slave_id", 1), 1, 1, 247),
                "timeout": _bounded_float(request.POST.get("manual_timeout", 3), 3, 1, 10),
                "byteOrder": byte_order if byte_order in {"BIG", "LITTLE"} else "BIG",
                "wordOrder": word_order if word_order in {"BIG", "LITTLE"} else "BIG",
            }
            if protocol == "modbus_tcp":
                host = request.POST.get("manual_host", "").strip()
                try:
                    parsed_host = ipaddress.ip_address(host)
                except ValueError:
                    messages.error(request, "Enter a valid equipment IP address.")
                    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
                if (
                    parsed_host.is_unspecified
                    or parsed_host.is_loopback
                    or parsed_host.is_multicast
                    or parsed_host.is_reserved
                ):
                    messages.error(request, "Enter a safe, reachable equipment IP address.")
                    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
                host = str(parsed_host)
                connection.update(
                    {
                        "host": host,
                        "port": _bounded_int(request.POST.get("manual_port", 502), 502, 1, 65535),
                    }
                )
                port_key = f"{connection['host']}:{connection['port']}"
            else:
                serial_port = request.POST.get("manual_serial_port", "").strip()
                if not serial_port:
                    messages.error(request, "Select the connected RS485 interface.")
                    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
                parity = request.POST.get("manual_parity", "N")
                connection.update(
                    {
                        "serial_port": serial_port,
                        "baudrate": _bounded_int(request.POST.get("manual_baudrate", 9600), 9600, 300, 921600),
                        "parity": parity if parity in {"N", "E", "O"} else "N",
                        "stopbits": _bounded_int(request.POST.get("manual_stopbits", 1), 1, 1, 2),
                    }
                )
                port_key = connection["serial_port"]
            name = request.POST.get("manual_name", "").strip()
            manufacturer = request.POST.get("manual_manufacturer", "").strip()
            model_number = request.POST.get("manual_model", "").strip()
            if not name:
                messages.error(request, "Equipment name is required.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            from apps.subscriptions.enforcement import can_add_device, get_device_limit_for_team

            if not can_add_device(request.team):
                limit = get_device_limit_for_team(request.team)
                messages.error(
                    request,
                    f"Your current plan supports up to {limit} equipment "
                    f"item{'s' if limit != 1 else ''}. Upgrade your plan or remove unused equipment first.",
                )
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            device_type = request.POST.get("manual_device_type", "other")
            if device_type not in {choice[0] for choice in DeviceTemplate.DEVICE_TYPE_CHOICES}:
                device_type = "other"
            template = DeviceTemplate.objects.create(
                name=f"{manufacturer} {model_number}".strip() or f"{name} template",
                manufacturer=manufacturer,
                model_number=model_number,
                device_type=device_type,
                protocol=protocol,
                mapping_strategy="site_defined",
                register_map={},
                source="user_created",
                created_by_team=request.team,
                is_verified=False,
            )
            device = Device.objects.create(
                team=request.team,
                gateway=gateway,
                site=gateway.site,
                port=port_key,
                name=name,
                template=template,
                device_type=template.device_type,
                protocol=protocol,
                connection_config=connection,
                discovery_meta={"connection": protocol, "interface": port_key},
                metadata={"guided_setup_validation": "pending"},
            )
            raw_draft_index = request.POST.get("draft_index", "").strip()
            try:
                draft_index = int(raw_draft_index) if raw_draft_index else None
                discovered_candidate = (
                    dict(discovered_devices[draft_index]) if draft_index is not None else {}
                )
            except (IndexError, TypeError, ValueError):
                draft_index = None
                discovered_candidate = {}
            candidate_data = {
                **discovered_candidate,
                "signature": discovered_candidate.get("signature") or name,
                "customer_name": name,
                "connection": protocol,
                "interface": port_key,
            }
            if draft_index is not None:
                item = create_or_update_candidate_item(
                    run=run,
                    index=draft_index,
                    candidate=candidate_data,
                )
                item.device = device
                item.candidate_data = candidate_data
                item.selected_template = template
                item.connection = connection
                item.confidence_explanation = "Configured manually by the customer."
            else:
                item = DeploymentSetupItem(
                    team=request.team,
                    run=run,
                    device=device,
                    candidate_data=candidate_data,
                    selected_template=template,
                    connection=connection,
                    confidence_score=0,
                    confidence_explanation="Configured manually by the customer.",
                )
            from apps.devices.datapoint_maps import register_map_to_datapoints, save_device_datapoint_map

            mapping = save_device_datapoint_map(
                device=device,
                team=request.team,
                datapoints=register_map_to_datapoints(register_map),
            )
            item.datapoints = mapping.datapoints
            item.state = DeploymentSetupItem.State.TEMPLATE_SELECTED
            item.save()
            request.session["onboarding_device_id"] = device.pk
            messages.info(request, "Manual setup saved as a private draft. Validate the live readings next.")
            return redirect(
                "web_team:devices:device_datapoint_mapping",
                team_slug=team_slug,
                pk=device.pk,
            )

        if action == "request_template":
            documentation_file = request.FILES.get("documentation_file")
            if documentation_file and (
                not documentation_file.name.lower().endswith(".pdf")
                or documentation_file.size > 10 * 1024 * 1024
                or documentation_file.content_type not in {"application/pdf", "application/x-pdf"}
            ):
                messages.error(request, "Equipment documentation must be a PDF no larger than 10 MB.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            if documentation_file:
                signature = documentation_file.read(5)
                documentation_file.seek(0)
                if signature != b"%PDF-":
                    messages.error(request, "The uploaded file is not a valid PDF document.")
                    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            request_manufacturer = request.POST.get("request_manufacturer", "").strip()
            request_model = request.POST.get("request_model", "").strip()
            request_protocol = request.POST.get("request_protocol", "modbus_tcp")
            if not request_manufacturer or not request_model:
                messages.error(request, "Manufacturer and model are required for a template request.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            if request_protocol not in {"modbus_tcp", "modbus_rtu"}:
                messages.error(request, "Choose Modbus TCP or Modbus RTU.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            equipment_request = EquipmentTemplateRequest.objects.create(
                team=request.team,
                run=run,
                manufacturer=request_manufacturer,
                model_number=request_model,
                protocol=request_protocol,
                documentation_url=request.POST.get("request_documentation_url", "").strip(),
                documentation_file=documentation_file,
                discovery_evidence=gateway.discovery_data,
            )
            raw_draft_index = request.POST.get("draft_index", "").strip()
            try:
                draft_index = int(raw_draft_index) if raw_draft_index else None
            except ValueError:
                draft_index = None
            draft_item = (
                run.items.filter(discovery_index=draft_index, device__isnull=True).first()
                if draft_index is not None
                else None
            )
            if draft_item:
                candidate_data = dict(draft_item.candidate_data or {})
                candidate_data.update(
                    {
                        "template_request_reference": str(equipment_request.support_reference),
                        "template_request_manufacturer": request_manufacturer,
                        "template_request_model": request_model,
                    }
                )
                draft_item.candidate_data = candidate_data
                draft_item.save(update_fields=["candidate_data", "updated_at"])
            append_setup_event(
                run,
                "template_requested",
                "A Novena equipment-template request was submitted.",
                actor=request.user,
                evidence={"support_reference": str(equipment_request.support_reference)},
            )
            messages.success(
                request,
                f"Template request submitted. Support reference: {equipment_request.support_reference}",
            )
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        if action == "deploy":
            if not guided_capable:
                messages.error(
                    request,
                    "Update this Gateway before deploying settings. "
                    "Secure remote setup is not available on its current software.",
                )
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            run = sync_setup_run(run)
            validated_items = list(
                run.items.filter(
                    state__in=[
                        DeploymentSetupItem.State.VALIDATED,
                        DeploymentSetupItem.State.TELEMETRY_CONFIRMED,
                    ],
                    device__isnull=False,
                )
            )
            if not validated_items:
                messages.warning(request, "Wait for at least one successful live validation before deploying.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            config_record = generate_and_push_config(gateway, setup_run=run)
            if not config_record:
                messages.error(request, "No validated equipment configuration was available to deploy.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            for item in validated_items:
                item.state = DeploymentSetupItem.State.QUEUED
                item.save(update_fields=["state", "updated_at"])
            run.state = DeploymentSetupRun.State.DEPLOYING
            run.current_step = "verify"
            run.save(update_fields=["state", "current_step", "updated_at"])
            append_setup_event(
                run,
                "configuration_queued",
                "Validated equipment configuration was queued for the Gateway.",
                actor=request.user,
                evidence={
                    "request_id": str(config_record.request_id),
                    "revision": config_record.revision,
                    "checksum": config_record.checksum,
                },
            )
            request.session["onboarding_device_id"] = validated_items[0].device_id
            return redirect("web_team:onboarding:step_4_alert", team_slug=team_slug)

    scan_state = discovery_scan_state(run)
    can_deploy = run.items.filter(
        state__in=[DeploymentSetupItem.State.VALIDATED, DeploymentSetupItem.State.TELEMETRY_CONFIRMED],
        device__isnull=False,
    ).exists()
    commissioning = build_commissioning_context(request.team, gateway=gateway, session=request.session)
    template_workflow = request.GET.get("workflow", "")
    if template_workflow not in {"custom", "request"}:
        template_workflow = ""
    try:
        active_candidate_index = int(request.GET.get("candidate", ""))
    except (TypeError, ValueError):
        active_candidate_index = None
    active_candidate = next(
        (
            candidate
            for candidate in commissioning["device_candidates"]
            if candidate["index"] == active_candidate_index
        ),
        None,
    )
    context = {
        "steps": ONBOARDING_STEPS,
        "current_step": 3,
        "gateway": gateway,
        "discovered_devices": discovered_devices,
        "templates": templates,
        "profile": profile,
        "setup_run": run,
        "setup_items": run.items.select_related("device", "selected_template", "validation_command"),
        "config_preview": human_config_preview(gateway),
        "guided_setup_available": guided_capable,
        "scan_state": scan_state,
        "can_deploy": can_deploy,
        "commissioning": commissioning,
        "template_workflow": template_workflow if active_candidate else "",
        "active_candidate": active_candidate,
    }
    return render(request, "onboarding/step_3_discover.html", context)


@require_permission("view_devices")
def discovery_poll(request, team_slug):
    """HTMX endpoint: returns discovery device list fragment."""
    from apps.devices.config_generator import human_config_preview

    gateway_id = request.session.get("onboarding_gateway_id")
    if not gateway_id:
        return render(request, "onboarding/partials/discovery_devices.html", {"discovered_devices": []})
    gateway = Gateway.objects.filter(id=gateway_id, team=request.team).first()
    discovered_devices = (gateway.discovery_data or {}).get("devices", []) if gateway else []
    profile = get_site_profile(gateway.site) if gateway else profile_for_request(request)
    templates = rank_templates_for_profile(visible_templates_for_team(request.team), profile)
    setup_run = (
        DeploymentSetupRun.objects.filter(team=request.team, gateway=gateway).order_by("-created_at").first()
        if gateway
        else None
    )
    if setup_run:
        from apps.devices.deployment_setup import discovery_scan_state, sync_setup_run

        setup_run = _reattach_running_discovery(setup_run)
        setup_run = sync_setup_run(setup_run)
        scan_state = discovery_scan_state(setup_run)
    else:
        scan_state = {
            "key": "idle",
            "title": "Ready to scan",
            "message": "Connect your equipment to the Gateway, then start a scan.",
        }
    can_deploy = bool(
        setup_run
        and setup_run.items.filter(
            state__in=[DeploymentSetupItem.State.VALIDATED, DeploymentSetupItem.State.TELEMETRY_CONFIRMED],
            device__isnull=False,
        ).exists()
    )
    return render(
        request,
        "onboarding/partials/discovery_region.html",
        {
            "discovered_devices": discovered_devices,
            "templates": templates,
            "gateway": gateway,
            "profile": profile,
            "setup_run": setup_run,
            "setup_items": setup_run.items.select_related("device", "selected_template") if setup_run else [],
            "scan_state": scan_state,
            "guided_setup_available": bool(gateway and "guided_setup_v1" in set(gateway.gateway_capabilities or [])),
            "include_scan_oob": True,
            "can_deploy": can_deploy,
            "config_preview": human_config_preview(gateway) if gateway else {},
            "commissioning": build_commissioning_context(request.team, gateway=gateway, session=request.session),
        },
    )


@require_permission("manage_devices")
def step_3_device(request, team_slug):
    """Compatibility redirect: equipment setup is consolidated in Guided Setup."""
    messages.info(request, "Equipment templates and manual setup are now available in Guided Setup.")
    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)


@require_permission("manage_devices")
def step_4_alert(request, team_slug):
    device_id = request.session.get("onboarding_device_id")
    if not device_id:
        return redirect("web_team:onboarding:step_3_device", team_slug=team_slug)
    device = get_object_or_404(Device, id=device_id, team=request.team)

    profile = get_site_profile(device.site)
    recommended_alerts = [
        {
            "name": preset.name,
            "key": preset.key,
            "condition": preset.condition,
            "threshold": preset.threshold,
            "severity": preset.severity,
            "duration_seconds": preset.duration_seconds,
            "create_maintenance_ticket": preset.create_maintenance_ticket,
        }
        for preset in recommended_alerts_for_device(device, profile.key)
    ]

    # Alert rules are often multiple, but the wizard still supports one manual fallback.
    existing_rule = AlertRule.objects.filter(device=device).first()
    from apps.devices.config_generator import human_config_preview
    from apps.devices.deployment_setup import deployment_progress, sync_setup_run

    setup_run = (
        DeploymentSetupRun.objects.filter(team=request.team, gateway=device.gateway).order_by("-created_at").first()
    )
    if setup_run:
        setup_run = sync_setup_run(setup_run)

    if request.method == "POST":
        alert_choice = ""
        if request.POST.get("accept_profile_presets", "1") == "1":
            apply_solution_profile_presets(device.site, request.user)
            alert_choice = "recommended"
        else:
            key = request.POST.get("key", "active_power")
            threshold = request.POST.get("threshold")
            if threshold:
                if existing_rule:
                    existing_rule.telemetry_key = key
                    existing_rule.threshold = float(threshold)
                    existing_rule.name = f"{device.name} High {key.replace('_', ' ').title()}"
                    existing_rule.save()
                    if existing_rule.notify_email and not existing_rule.recipients.exists():
                        existing_rule.recipients.add(request.user)
                else:
                    rule = AlertRule.objects.create(
                        team=request.team,
                        device=device,
                        name=f"{device.name} High {key.replace('_', ' ').title()}",
                        telemetry_key=key,
                        condition="gt",
                        threshold=float(threshold),
                        severity="critical",
                    )
                    rule.recipients.add(request.user)
                alert_choice = "manual"
        if alert_choice and setup_run:
            from apps.devices.deployment_setup import append_setup_event

            if not setup_run.events.filter(event_type="alerts_reviewed").exists():
                append_setup_event(
                    setup_run,
                    "alerts_reviewed",
                    "Recommended alert settings were reviewed.",
                    actor=request.user,
                    evidence={"choice": alert_choice},
                )
            setup_run = sync_setup_run(setup_run)
            if setup_run.state == DeploymentSetupRun.State.VERIFYING:
                setup_run = sync_setup_run(setup_run)
            if setup_run.state not in {
                DeploymentSetupRun.State.COMPLETED,
                DeploymentSetupRun.State.COMPLETED_ATTENTION,
            }:
                messages.info(
                    request,
                    "Alert choices are saved. Novena is still waiting for the Gateway and first live data.",
                )
                return redirect("web_team:onboarding:step_4_alert", team_slug=team_slug)
        if alert_choice:
            return redirect("web_team:onboarding:complete", team_slug=team_slug)

    context = {
        "steps": ONBOARDING_STEPS,
        "current_step": 4,
        "device": device,
        "rule": existing_rule,
        "profile": profile,
        "recommended_alerts": recommended_alerts,
        "setup_run": setup_run,
        "deployment_progress": deployment_progress(setup_run) if setup_run else [],
        "config_preview": human_config_preview(device.gateway),
        "commissioning": build_commissioning_context(request.team, gateway=device.gateway, session=request.session),
    }
    return render(request, "onboarding/step_4_alert.html", context)


@require_permission("manage_devices")
def complete(request, team_slug):
    site_id = request.session.get("onboarding_site_id")
    gateway_id = request.session.get("onboarding_gateway_id")
    device_id = request.session.get("onboarding_device_id")
    gateway = Gateway.objects.filter(id=gateway_id, team=request.team).first() if gateway_id else None
    commissioning = build_commissioning_context(request.team, gateway=gateway, session=request.session)
    setup_run = commissioning.get("setup_run")
    if setup_run and setup_run.state not in {
        DeploymentSetupRun.State.COMPLETED,
        DeploymentSetupRun.State.COMPLETED_ATTENTION,
    }:
        messages.info(
            request,
            "Guided Setup is still verifying the Gateway and first live data.",
        )
        return redirect("web_team:onboarding:step_4_alert", team_slug=team_slug)

    impact_profile = None
    if site_id and flag_is_active(request, "business_impact_roi"):
        from apps.impact.services import ensure_site_profile, suggest_data_sources

        site = Site.objects.filter(id=site_id, team=request.team).first()
        if site:
            impact_profile = ensure_site_profile(site)
            suggest_data_sources(site)

    for key in [
        "onboarding_site_id",
        "onboarding_gateway_id",
        "onboarding_device_id",
        "setup_mode",
        "connectivity_type",
        "solution_profile",
    ]:
        if key in request.session:
            del request.session[key]

    return render(
        request,
        "onboarding/complete.html",
        {
            "steps": ONBOARDING_STEPS,
            "current_step": 5,
            "gateway": gateway,
            "device_id": device_id,
            "commissioning": commissioning,
            "setup_run": setup_run,
            "impact_profile": impact_profile,
        },
    )


@require_permission("view_devices")
def support_bundle_download(request, team_slug, run_id):
    from apps.devices.deployment_setup import support_bundle

    run = get_object_or_404(
        DeploymentSetupRun.objects.select_related("gateway", "site"),
        run_id=run_id,
        team=request.team,
    )
    response = HttpResponse(
        json.dumps(support_bundle(run), cls=DjangoJSONEncoder, indent=2),
        content_type="application/json",
    )
    response["Content-Disposition"] = f'attachment; filename="novena-setup-{run.run_id}.json"'
    return response


# Setup Wizard Views for Existing Customers


@require_permission("manage_devices")
def setup_start(request, team_slug):
    request.session["setup_mode"] = True
    return render(request, "onboarding/setup_start.html")


@require_permission("manage_devices")
def setup_step_site(request, team_slug):
    sites = Site.objects.filter(team=request.team)

    if request.method == "POST":
        site_id = request.POST.get("site_id")
        if site_id == "new":
            return redirect("web_team:onboarding:step_profile", team_slug=team_slug)
        elif site_id:
            request.session["onboarding_site_id"] = int(site_id)
            site = Site.objects.filter(id=site_id, team=request.team).first()
            if site:
                request.session["solution_profile"] = site.solution_profile
            return redirect("web_team:onboarding:step_connectivity", team_slug=team_slug)

    context = {"steps": ONBOARDING_STEPS, "current_step": 1, "sites": sites}
    return render(request, "onboarding/setup_step_site.html", context)


@require_permission("manage_devices")
def setup_step_connectivity(request, team_slug):
    if request.method == "POST":
        connectivity = request.POST.get("connectivity")
        if connectivity == "gateway":
            request.session["connectivity_type"] = connectivity
            return redirect("web_team:onboarding:step_2_gateway", team_slug=team_slug)
        if connectivity == "direct":
            request.session.pop("connectivity_type", None)
            messages.warning(
                request,
                "Direct cloud onboarding is coming soon. Please use Novena Gateway setup for now.",
            )
            return redirect("web_team:onboarding:step_connectivity", team_slug=team_slug)

        messages.error(request, "Choose Novena Gateway to continue setup.")
        return redirect("web_team:onboarding:step_connectivity", team_slug=team_slug)

    context = {"steps": ONBOARDING_STEPS, "current_step": 2}  # We'll reuse the progress bar
    return render(request, "onboarding/setup_step_connectivity.html", context)
