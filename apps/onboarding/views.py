import ipaddress
import json
import re
import uuid
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from waffle import flag_is_active

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
    get_profile,
    get_site_profile,
    profile_for_request,
    rank_templates_for_profile,
)
from apps.teams.decorators import require_permission

ONBOARDING_STEPS = [
    {"num": 1, "label": "Location"},
    {"num": 2, "label": "Gateway"},
    {"num": 3, "label": "Equipment"},
    {"num": 4, "label": "Verify"},
    {"num": 5, "label": "Finish"},
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


def _candidate_connection_from_post(post, candidate, field_key, *, strict=True):
    """Apply customer-edited connection values to one discovered candidate."""
    from apps.devices.deployment_setup import connection_from_candidate

    candidate = dict(candidate)
    connection = connection_from_candidate(candidate)
    raw_slave_id = post.get(f"slave_id_{field_key}", connection.get("slave_id") or 1)
    try:
        slave_id = int(raw_slave_id)
    except (TypeError, ValueError):
        if strict:
            raise ValueError("Equipment unit ID must be between 1 and 247") from None
        slave_id = raw_slave_id
    if isinstance(slave_id, int) and not 1 <= slave_id <= 247:
        if strict:
            raise ValueError("Equipment unit ID must be between 1 and 247")
    elif not isinstance(slave_id, int) and strict:
        raise ValueError("Equipment unit ID must be between 1 and 247")
    connection["slave_id"] = slave_id
    protocol = candidate.get("connection") or candidate.get("protocol")
    if protocol == "modbus_tcp":
        host = post.get(f"host_{field_key}", connection.get("host", "")).strip()
        try:
            parsed_host = ipaddress.ip_address(host)
        except ValueError:
            if strict:
                raise ValueError("Enter a valid equipment IP address") from None
            parsed_host = None
        if parsed_host and (parsed_host.is_unspecified or parsed_host.is_loopback or parsed_host.is_multicast):
            if strict:
                raise ValueError("Enter a safe, reachable equipment IP address")
            parsed_host = None
        raw_port = post.get(f"port_{field_key}", connection.get("port", 502))
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            if strict:
                raise ValueError("Modbus TCP port must be between 1 and 65535") from None
            port = raw_port
        if isinstance(port, int) and not 1 <= port <= 65535:
            if strict:
                raise ValueError("Modbus TCP port must be between 1 and 65535")
        elif not isinstance(port, int) and strict:
            raise ValueError("Modbus TCP port must be between 1 and 65535")
        normalized_host = str(parsed_host) if parsed_host else host
        connection.update({"host": normalized_host, "port": port})
        candidate.update(
            {
                "host": normalized_host,
                "port": port,
                "slave_id": slave_id,
                "interface": f"{normalized_host}:{port}" if normalized_host else "",
            }
        )
    else:
        candidate["slave_id"] = slave_id
    return connection, candidate


def _candidate_for_ref(discovered_devices, run, candidate_ref, expected_key=""):
    """Resolve a current scan row or a retained saved draft without trusting row order."""
    from apps.devices.deployment_setup import equipment_candidate_key

    candidate_ref = str(candidate_ref or "")
    if candidate_ref.startswith("saved-"):
        try:
            item_id = int(candidate_ref.removeprefix("saved-"))
        except ValueError as exc:
            raise IndexError("Invalid saved equipment reference") from exc
        item = run.items.filter(pk=item_id, device__isnull=True, removed_at__isnull=True).first()
        if not item or not item.candidate_data:
            raise IndexError("Equipment draft is no longer available")
        candidate = dict(item.candidate_data)
        actual_key = item.candidate_key or equipment_candidate_key(candidate, item.connection)
        if expected_key and actual_key != expected_key:
            raise ValueError("The equipment list changed. Review this row again before saving.")
        return candidate, item.discovery_index, item

    try:
        index = int(candidate_ref)
    except ValueError as exc:
        raise IndexError("Invalid equipment reference") from exc
    if not 0 <= index < len(discovered_devices):
        raise IndexError("Equipment is no longer present in the latest scan")
    candidate = dict(discovered_devices[index])
    candidate_key = equipment_candidate_key(candidate)
    if expected_key and candidate_key != expected_key:
        raise ValueError("The equipment list changed after this page loaded. Review it again before saving.")
    item = (
        run.items.filter(candidate_key=candidate_key, device__isnull=True, removed_at__isnull=True).first()
        if candidate_key
        else None
    )
    if not item:
        legacy = run.items.filter(
            discovery_index=index,
            device__isnull=True,
            candidate_key="",
            removed_at__isnull=True,
        ).first()
        if legacy and (
            not equipment_candidate_key(legacy.candidate_data, legacy.connection)
            or equipment_candidate_key(legacy.candidate_data, legacy.connection) == candidate_key
        ):
            item = legacy
    return candidate, index, item


def _reattach_running_discovery(run):
    """Keep Guided Setup aligned with a Gateway scan that is already running."""
    from apps.devices.deployment_setup import discovery_report_is_stale

    discovery = run.gateway.discovery_data or {}
    scan_id = str(discovery.get("scan_id") or "")
    if discovery.get("status") != "running" or not scan_id or discovery_report_is_stale(discovery):
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
    from apps.devices.deployment_setup import DISCOVERY_REPORT_STALE_AFTER, discovery_report_is_stale

    discovery_meta = (run.summary or {}).get("discovery") or {}
    scan_id = str(discovery_meta.get("active_scan_id") or "")
    if not scan_id:
        return False

    discovery = run.gateway.discovery_data or {}
    if str(discovery.get("scan_id") or "") == scan_id:
        return discovery.get("status") == "running" and not discovery_report_is_stale(discovery)

    started_at_raw = discovery_meta.get("started_at")
    if started_at_raw:
        try:
            started_at = timezone.datetime.fromisoformat(str(started_at_raw).replace("Z", "+00:00"))
            if timezone.is_naive(started_at):
                started_at = timezone.make_aware(started_at, UTC)
            started_at = started_at.astimezone(UTC)
        except (TypeError, ValueError):
            return False
        if timezone.now() - started_at > DISCOVERY_REPORT_STALE_AFTER:
            return False

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

    if started_at_raw:
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

    return render(request, "onboarding/step_2b_wait.html", _gateway_wait_context(request, gateway))


def _gateway_wait_context(request, gateway):
    """Build the same readiness state for the first page and each live refresh."""

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
    # Keep a completed preflight result in the setup run, but always show
    # current connection evidence to the customer on this live wait page.
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

    # A newly claimed Gateway has not had time to activate or send its first
    # operational heartbeat. Missing evidence is pending, not seven faults.
    activation = gateway.activations.order_by("-created_at").first()
    freshness = gateway.freshness
    awaiting_first_heartbeat = freshness.status != "live" and (
        gateway.last_seen is None
        or (activation is not None and gateway.last_seen < activation.created_at)
    )
    wait_started_at = activation.created_at if activation else run.created_at
    wait_seconds = (timezone.now() - wait_started_at).total_seconds()
    connection_delayed = awaiting_first_heartbeat and wait_seconds >= 90
    blocking_failures = {
        check["key"] for check in readiness["checks"] if check["blocking"] and check["status"] == "fail"
    }
    clock_settling = (
        freshness.status == "live"
        and blocking_failures == {"clock"}
        and wait_seconds < 180
    )
    display_checks = readiness["checks"]
    if awaiting_first_heartbeat:
        display_checks = [
            {
                **check,
                "status": "pending",
                "message": "Waiting for the Gateway to report this check.",
                "action": "",
            }
            for check in display_checks
        ]
    elif freshness.status != "live":
        # Downstream results are last-known evidence when check-ins stop.
        # Show one connection issue, then wait for fresh reports.
        display_checks = [
            check
            if check["key"] == "gateway_online"
            else {
                **check,
                "status": "pending",
                "message": "Waiting for a fresh Gateway report.",
                "action": "",
            }
            for check in display_checks
        ]
    elif clock_settling:
        display_checks = [
            {
                **check,
                "status": "pending",
                "message": "Finishing secure setup. This updates automatically.",
                "action": "",
            }
            if check["key"] in {"clock", "guided_setup"} and check["status"] != "pass"
            else check
            for check in display_checks
        ]
    can_retry_activation = bool(
        connection_delayed
        and activation
        and activation.status in {"provisioning", "pending", "delivered", "retried", "retry", "failed"}
        and activation.expires_at > timezone.now()
    )
    guidance = _gateway_wait_guidance(
        gateway,
        readiness,
        freshness.status,
        awaiting_first_heartbeat=awaiting_first_heartbeat,
        connection_delayed=connection_delayed,
        clock_settling=clock_settling,
        can_retry_activation=can_retry_activation,
    )

    return {
        "steps": ONBOARDING_STEPS,
        "current_step": 2,
        "gateway": gateway,
        "profile": get_site_profile(gateway.site),
        "commissioning": build_commissioning_context(request.team, gateway=gateway, session=request.session),
        "setup_run": run,
        "readiness": readiness,
        "display_checks": display_checks,
        "guidance": guidance,
    }


def _gateway_wait_guidance(
    gateway,
    readiness,
    freshness_status,
    *,
    awaiting_first_heartbeat,
    connection_delayed,
    clock_settling,
    can_retry_activation,
):
    """Choose one customer action from the strongest available evidence."""
    if awaiting_first_heartbeat:
        if connection_delayed:
            return {
                "tone": "attention",
                "title": "We are still waiting for your Gateway",
                "message": "Your claim is saved. We have not received its first check-in yet.",
                "steps": [
                    "Check that the Gateway is powered on.",
                    "Check that its Ethernet or Wi-Fi connection reaches your site network.",
                    "If it remains here, ask your site IT team to check internet access, "
                    "or contact Novena support with the serial number below.",
                ],
                "show_retry": can_retry_activation,
            }
        return {
            "tone": "waiting",
            "title": "Gateway claimed. Connecting securely…",
            "message": (
                "Keep this page open. The Gateway will check in automatically; "
                "you do not need to enter the claim code again."
            ),
            "steps": [],
            "show_retry": False,
        }
    if clock_settling:
        return {
            "tone": "waiting",
            "title": "Gateway connected. Finishing secure setup…",
            "message": "The Gateway is synchronizing its clock. This page will update automatically.",
            "steps": [],
            "show_retry": False,
        }
    if readiness["status"] == "ready":
        warnings = any(check["status"] == "warning" for check in readiness["checks"])
        return {
            "tone": "ready",
            "title": "Gateway ready for equipment",
            "message": (
                "You can continue. Optional notes below will not stop setup."
                if warnings
                else "Connection checks passed. Continue to connect your equipment."
            ),
            "steps": [],
            "show_retry": False,
        }
    if freshness_status != "live":
        title = "Gateway connection interrupted"
        message = "The Gateway checked in before, but its latest check-in is delayed. Your setup is saved."
        steps = [
            "Check that the Gateway is powered on and connected to your site network.",
            "If your internet connection was interrupted, wait for it to recover. "
            "This page checks again automatically.",
            "If it stays disconnected, contact Novena support with the serial number below.",
        ]
    elif gateway.internet_reachable is False:
        title = "Check your site's internet connection"
        message = "The Gateway is online locally, but reports that it cannot reach the internet."
        steps = [
            "Ask your site IT team to check internet access for the Gateway.",
            "This page will update when the connection recovers.",
        ]
    elif gateway.dns_ok is False:
        title = "Check your site's network settings"
        message = "The Gateway reports that it cannot look up the Novena service address."
        steps = [
            "Ask your site IT team to check DNS for the Gateway.",
            "This page will update when the connection recovers.",
        ]
    elif gateway.tls_ok is False:
        title = "Secure connection needs attention"
        message = "The Gateway is online, but its security check did not pass."
        steps = ["Contact Novena support with the serial number below. We will check the secure connection with you."]
    elif gateway.broker_tcp_ok is False or not gateway.mqtt_connected:
        title = "Gateway cannot reach Novena yet"
        message = "The Gateway is running, but its connection to Novena has not completed."
        steps = [
            "Ask your site IT team to check that the Gateway can reach the Novena service from this network.",
            "If the network looks healthy, contact Novena support with the serial number below.",
        ]
    elif not gateway.remote_control_clock_ready:
        title = "Gateway time needs attention"
        message = "The Gateway is online, but secure setup is waiting for its clock to synchronize."
        steps = [
            "Keep it connected to the internet.",
            "If this does not clear, contact Novena support with the serial number below.",
        ]
    else:
        title = "Gateway needs a setup check"
        message = "One connection check still needs attention. Your setup is saved."
        steps = ["Review the highlighted check, then contact Novena support if it does not clear."]
    return {"tone": "attention", "title": title, "message": message, "steps": steps, "show_retry": False}


@require_permission("manage_devices")
def gateway_status_poll(request, team_slug):
    """Refresh the full Gateway progress, readiness, and Continue control."""
    gateway_id = request.session.get("onboarding_gateway_id")
    if not gateway_id:
        gateway_url = reverse("web_team:onboarding:step_2_gateway", args=[team_slug])
        return HttpResponse(status=204, headers={"HX-Redirect": gateway_url})
    gateway = get_object_or_404(Gateway, id=gateway_id, team=request.team)
    return render(
        request,
        "onboarding/partials/gateway_live.html",
        _gateway_wait_context(request, gateway),
    )


def _equipment_progress(run, commissioning, scan_state, can_deploy):
    """Summarize only the milestones a customer can complete on the Equipment step."""
    listed = bool(commissioning["equipment_count"])
    template_selected = bool(
        can_deploy
        or run.items.filter(selected_template__isnull=False, removed_at__isnull=True).exists()
    )
    tested = bool(can_deploy or commissioning["confirmation_equipment_count"])
    if can_deploy:
        message = (
            "At least one equipment template and its live readings were confirmed. "
            "Deploy it or review more equipment."
        )
    elif commissioning["confirmation_equipment_count"]:
        message = "Live readings are ready. Review their values and confirm the template for each equipment item."
    elif commissioning["validation_equipment_count"]:
        message = "Checking live readings now. Results will appear automatically."
    elif not listed and scan_state["key"] == "scanning":
        message = "Finding connected equipment. Results will appear automatically."
    elif not listed:
        message = "Scan for equipment or add it manually to begin."
    elif not template_selected:
        message = "Preview the matching template, then save the equipment or test its live readings."
    else:
        message = "Test the template's live readings, then confirm they match your equipment."
    return {
        "listed": listed,
        "template_selected": template_selected,
        "tested": tested,
        "verified": can_deploy,
        "message": message,
    }


@require_permission("view_devices")
def equipment_template_preview(request, team_slug):
    """Expose a team-visible template's read-only signal map before a live test."""
    from apps.devices.config_generator import normalized_datapoints

    raw_template_id = request.GET.get("template_id", "")
    template_id = int(raw_template_id) if raw_template_id.isdecimal() and len(raw_template_id) <= 10 else None
    template = visible_templates_for_team(request.team).filter(pk=template_id).first() if template_id else None
    if not template:
        return JsonResponse({"error": "Template unavailable. Choose another template."}, status=404)
    signals = [
        {
            "key": point["key"],
            "label": point["label"],
            "address": point["address"],
            "function_code": point["functionCode"],
            "data_type": point["data_type"],
            "unit": point["unit"],
            "scale": point["scale"],
            "offset": point["offset"],
            "objects_count": point["objectsCount"],
        }
        for point in normalized_datapoints(template)
        if point.get("access") in {"read", "read_only", "read_write"}
    ]
    signals.sort(
        key=lambda point: _signal_display_order(template.register_map, point["key"], point["label"])
    )
    return JsonResponse(
        {
            "manufacturer": template.manufacturer,
            "model": template.model_number,
            "protocol": template.get_protocol_display(),
            "mapping_strategy": template.mapping_strategy,
            "signals": signals,
        }
    )


def _signal_display_order(register_map, key, label):
    register = (register_map or {}).get(key)
    role = register.get("dashboard_role") if isinstance(register, dict) else None
    return ({"primary": 0, "secondary": 1, "health": 2}.get(role, 3), label)


def _attach_equipment_readings(commissioning):
    """Pair each live result with the map the Gateway read, for customer review."""
    for item in commissioning["equipment_setup_items"]:
        points = {point.get("key"): point for point in (item.datapoints or [])}
        item.reading_rows = [
            {
                "key": signal.get("key", ""),
                "label": points.get(signal.get("key"), {}).get("label") or signal.get("key", "Signal"),
                "address": signal.get("address", points.get(signal.get("key"), {}).get("address")),
                "function_code": points.get(signal.get("key"), {}).get("functionCode"),
                "raw_value": signal.get("raw_value"),
                "value": signal.get("decoded_value", signal.get("value")),
                "unit": signal.get("unit") or points.get(signal.get("key"), {}).get("unit", ""),
                "status": signal.get("status", "unknown"),
                "warning": signal.get("warning_message") or signal.get("error_message") or "",
            }
            for signal in (item.validation_result or {}).get("signals", [])
        ]
        roles = (item.selected_template.register_map or {}) if item.selected_template else {}
        item.reading_rows.sort(
            key=lambda row: _signal_display_order(roles, row["key"], row["label"])
        )


def _require_customer_confirmation_for_predeployment(run):
    """Bring older technical-only passes into the explicit customer review flow."""
    if run.configurations.exists():
        return
    from apps.devices.deployment_setup import append_setup_event

    for item in run.items.filter(
        state=DeploymentSetupItem.State.VALIDATED, removed_at__isnull=True
    ).select_related("device"):
        result = dict(item.validation_result or {})
        if result.get("customer_confirmed_at"):
            continue
        has_readings = result.get("status") == "success" and bool(result.get("signals"))
        item.state = (
            DeploymentSetupItem.State.AWAITING_CONFIRMATION
            if has_readings else DeploymentSetupItem.State.NEEDS_ATTENTION
        )
        item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
        if not has_readings:
            result["message"] = "Test live readings, then confirm they match your equipment."
        item.validation_result = result
        item.save(update_fields=["state", "trust_level", "validation_result", "updated_at"])
        if item.device:
            metadata = dict(item.device.metadata or {})
            metadata["guided_setup_validation"] = "awaiting_confirmation" if has_readings else "pending"
            item.device.metadata = metadata
            item.device.save(update_fields=["metadata", "updated_at"])
        append_setup_event(
            run, "customer_confirmation_required",
            "Earlier live check needs customer confirmation before deployment.", item=item,
        )


@require_permission("manage_devices")
def step_3_discover(request, team_slug):
    """Automatic-first equipment setup with guided manual fallback."""
    from apps.devices.config_generator import (
        generate_and_push_config,
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
    _require_customer_confirmation_for_predeployment(run)
    guided_capable = gateway_supports_guided_setup(gateway)

    if request.method == "POST" and request.POST.get("action") == "confirm_setup_item":
        with transaction.atomic():
            item = get_object_or_404(
                run.items.select_related("device", "selected_template").select_for_update(of=("self",)),
                pk=request.POST.get("item_id"), removed_at__isnull=True, device__isnull=False,
            )
            result = dict(item.validation_result or {})
            signals = result.get("signals") or []
            if (
                item.state != DeploymentSetupItem.State.AWAITING_CONFIRMATION
                or not item.selected_template
                or item.selected_template.mapping_strategy == "site_defined"
                or result.get("status") != "success"
                or not signals
                or any(signal.get("status") not in {"success", "warning"} for signal in signals)
            ):
                messages.error(request, "Complete a successful live test before confirming this template.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            from apps.devices.deployment_setup import append_setup_event

            item.state = DeploymentSetupItem.State.VALIDATED
            item.trust_level = (
                DeploymentSetupItem.Trust.NOVENA_VERIFIED
                if item.selected_template.is_verified else DeploymentSetupItem.Trust.CUSTOMER_VALIDATED
            )
            result["customer_confirmed_at"] = timezone.now().isoformat()
            result["customer_confirmed_by"] = request.user.pk
            item.validation_result = result
            item.save(update_fields=["state", "trust_level", "validation_result", "updated_at"])
            metadata = dict(item.device.metadata or {})
            metadata["guided_setup_validation"] = "validated"
            item.device.metadata = metadata
            item.device.save(update_fields=["metadata", "updated_at"])
            append_setup_event(
                run,
                "customer_template_confirmed",
                "Customer confirmed the live readings and template.",
                item=item,
                actor=request.user,
            )
            messages.success(request, "Template confirmed. This equipment is ready to deploy.")
        return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

    if request.method == "POST" and request.POST.get("action") == "restart_setup_item":
        with transaction.atomic():
            item = get_object_or_404(
                run.items.select_related("device").select_for_update(of=("self",)),
                pk=request.POST.get("item_id"), removed_at__isnull=True, device__isnull=False,
            )
            if (
                item.state not in {
                    DeploymentSetupItem.State.DISCOVERED,
                    DeploymentSetupItem.State.TEMPLATE_SELECTED,
                    DeploymentSetupItem.State.NEEDS_ATTENTION,
                    DeploymentSetupItem.State.FAILED,
                    DeploymentSetupItem.State.AWAITING_CONFIRMATION,
                    DeploymentSetupItem.State.VALIDATED,
                }
                or item.device.last_telemetry_at
                or run.configurations.exists()
            ):
                messages.error(request, "This equipment has already entered deployment. Manage it from Devices.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            provisional_device = item.device
            previous_template_id = item.selected_template_id
            item.device = None
            item.selected_template = None
            item.datapoints = []
            item.validation_result = {}
            item.validation_command = None
            item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
            item.state = DeploymentSetupItem.State.DISCOVERED
            item.removed_at = timezone.now()
            item.save()
            provisional_device.delete()
            from apps.devices.deployment_setup import append_setup_event

            append_setup_event(
                run,
                "equipment_setup_restarted",
                "Customer cleared predeployment equipment setup to start again.",
                item=item,
                actor=request.user,
                evidence={"previous_template_id": previous_template_id},
            )
        messages.success(request, "Equipment setup cleared. Review the scanned equipment to start again.")
        return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

    discovery_data = gateway.discovery_data or {}
    discovered_devices = discovery_data.get("devices", [])
    profile = get_site_profile(gateway.site)
    templates = rank_templates_for_profile(visible_templates_for_team(request.team), profile)

    if request.method == "POST":
        action = request.POST.get("action", "validate_selected")
        if action in {"save_setup_item", "retry_setup_item", "clear_setup_template"}:
            item = get_object_or_404(
                run.items.select_related("device", "selected_template"),
                pk=request.POST.get("item_id"),
                removed_at__isnull=True,
                device__isnull=False,
            )
            editable_states = {
                DeploymentSetupItem.State.DISCOVERED,
                DeploymentSetupItem.State.TEMPLATE_SELECTED,
                DeploymentSetupItem.State.NEEDS_ATTENTION,
                DeploymentSetupItem.State.FAILED,
                DeploymentSetupItem.State.VALIDATED,
                DeploymentSetupItem.State.AWAITING_CONFIRMATION,
            }
            if item.state not in editable_states or item.device.last_telemetry_at:
                messages.error(
                    request, "This equipment is already being validated or deployed. Refresh its status first."
                )
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

            template_id = "" if action == "clear_setup_template" else request.POST.get("template_id", "").strip()
            template = visible_templates_for_team(request.team).filter(pk=template_id).first() if template_id else None
            if template_id and not template:
                messages.error(request, "Choose a template available to your organisation.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            if template and template.protocol != item.device.protocol:
                messages.error(request, "Choose a template that matches this equipment connection type.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            if action == "retry_setup_item" and not template:
                messages.error(request, "Choose an equipment template before retrying the live check.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            if action == "retry_setup_item" and not guided_capable:
                messages.warning(request, "The Gateway is not ready for a secure live check. Keep it online and retry.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

            candidate = {
                **(item.candidate_data or {}),
                "connection": item.device.protocol,
                "host": item.connection.get("host", ""),
                "port": item.connection.get("port", 502),
                "slave_id": item.connection.get("slave_id", 1),
            }
            try:
                connection, candidate = _candidate_connection_from_post(request.POST, candidate, "setup", strict=True)
            except (TypeError, ValueError) as exc:
                messages.error(request, str(exc))
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            name = request.POST.get("equipment_name", "").strip() or item.device.name
            candidate["customer_name"] = name
            candidate["customer_draft_saved"] = True
            provisional_device = item.device
            previous_device_id = provisional_device.pk
            with transaction.atomic():
                item.candidate_data = candidate
                item.connection = connection
                item.selected_template = template
                item.datapoints = []
                item.validation_result = {}
                item.validation_command = None
                item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
                item.state = (
                    DeploymentSetupItem.State.TEMPLATE_SELECTED if template else DeploymentSetupItem.State.DISCOVERED
                )
                if template:
                    provisional_device.name = name
                    provisional_device.template = template
                    provisional_device.device_type = template.device_type
                    provisional_device.port = candidate.get("interface") or provisional_device.port
                    provisional_device.connection_config = connection
                    provisional_device.discovery_meta = candidate
                    metadata = dict(provisional_device.metadata or {})
                    metadata["guided_setup_validation"] = "pending"
                    provisional_device.metadata = metadata
                    provisional_device.save()
                else:
                    # This Device was only prepared for a predeployment live check.
                    # Keep the customer draft, then return it to the full Review flow.
                    item.device = None
                item.save()
                if not template:
                    provisional_device.delete()
                append_setup_event(
                    run,
                    "setup_item_edited",
                    "Customer updated an equipment template or connection before deployment.",
                    item=item,
                    actor=request.user,
                    evidence={
                        "template_id": template.pk if template else None,
                        "retry": action == "retry_setup_item",
                        "previous_device_id": previous_device_id,
                    },
                )
            if action == "retry_setup_item":
                try:
                    start_validation(item=item, template=template, requested_by=request.user)
                except ValueError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.info(request, "Checking live readings again. Results will appear here automatically.")
            else:
                messages.success(
                    request,
                    "Template removed. Choose the exact equipment model when ready."
                    if not template
                    else "Equipment changes saved. Run the live check when ready.",
                )
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        for candidate_action in (
            "save_candidate_draft",
            "save_and_validate",
            "skip_candidate",
            "resume_candidate",
            "start_custom_template",
            "request_candidate_template",
            "remove_candidate_draft",
        ):
            candidate_index = request.POST.get(candidate_action)
            if candidate_index is not None:
                action = f"{candidate_action}:{candidate_index}"
                break
        if action in {"start_discovery", "start_target_discovery"}:
            if not gateway_supports_guided_setup(gateway):
                if not gateway.remote_control_clock_ready:
                    messages.warning(
                        request,
                        "The Gateway is synchronizing its secure clock. Keep it online; "
                        "scanning will unlock automatically when it is ready.",
                    )
                else:
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
            "skip_candidate",
            "resume_candidate",
            "start_custom_template",
            "request_candidate_template",
            "remove_candidate_draft",
        }:
            try:
                candidate, index, existing_item = _candidate_for_ref(
                    discovered_devices,
                    run,
                    raw_draft_index,
                    request.POST.get(f"candidate_key_{raw_draft_index}", ""),
                )
            except (IndexError, TypeError, ValueError) as exc:
                messages.error(request, f"This equipment draft could not be saved: {exc}")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

            if draft_action == "remove_candidate_draft":
                if not existing_item:
                    messages.info(request, "This equipment does not have a saved draft to remove.")
                else:
                    existing_item.removed_at = timezone.now()
                    existing_item.save(update_fields=["removed_at", "updated_at"])
                    append_setup_event(
                        run,
                        "candidate_draft_removed",
                        "Equipment draft removed from Guided Setup.",
                        item=existing_item,
                        actor=request.user,
                        evidence={"candidate_key": existing_item.candidate_key},
                    )
                    messages.success(request, "Saved equipment draft removed.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

            try:
                connection, candidate = _candidate_connection_from_post(
                    request.POST, candidate, raw_draft_index, strict=False
                )
            except (TypeError, ValueError) as exc:
                messages.error(request, f"This equipment draft could not be saved: {exc}")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

            template = None
            template_id = request.POST.get(f"template_{raw_draft_index}", "").strip()
            if template_id:
                template = visible_templates_for_team(request.team).filter(pk=template_id).first()
                if not template:
                    messages.error(request, "Choose a template available to your organisation.")
                    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

            candidate["customer_name"] = (
                request.POST.get(f"name_{raw_draft_index}", "").strip()
                or candidate.get("customer_name")
                or candidate.get("signature")
                or "Equipment"
            )
            candidate["customer_draft_saved"] = True
            candidate["skipped_for_now"] = draft_action == "skip_candidate"
            try:
                item = create_or_update_candidate_item(
                    run=run,
                    index=index,
                    candidate=candidate,
                    existing_item=existing_item,
                    connection_override=connection,
                )
            except ValueError as exc:
                messages.error(request, f"This equipment draft could not be saved: {exc}")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            item.candidate_data = candidate
            item.connection = connection
            item.selected_template = template
            item.state = (
                DeploymentSetupItem.State.TEMPLATE_SELECTED if template else DeploymentSetupItem.State.DISCOVERED
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
            event_type = "candidate_skipped" if draft_action == "skip_candidate" else "candidate_draft_saved"
            event_message = (
                "Equipment was skipped for now."
                if draft_action == "skip_candidate"
                else "Equipment review saved as a draft."
            )
            append_setup_event(
                run,
                event_type,
                event_message,
                item=item,
                actor=request.user,
                evidence={"discovery_index": index, "template_id": template.pk if template else None},
            )

            if draft_action == "start_custom_template":
                return redirect(
                    f"{reverse('web_team:onboarding:step_3_discover', args=[team_slug])}"
                    f"?candidate={raw_draft_index}&workflow=custom#custom-template-panel-{raw_draft_index}"
                )
            if draft_action == "request_candidate_template":
                return redirect(
                    f"{reverse('web_team:onboarding:step_3_discover', args=[team_slug])}"
                    f"?candidate={raw_draft_index}&workflow=request#template-request-panel-{raw_draft_index}"
                )
            if draft_action == "skip_candidate":
                messages.success(request, "Equipment skipped for now. Your saved details will be here when you return.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            if draft_action == "resume_candidate":
                messages.success(request, "Equipment returned to your review list.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            messages.success(request, "Draft saved. You can return and choose a template later.")
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        if action == "cancel_discovery":
            try:
                from apps.devices.deployment_setup import discovery_report_is_stale
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
                report = gateway.discovery_data or {}
                gateway_still_scanning = (
                    str(report.get("scan_id") or "") == scan_id
                    and report.get("status") == "running"
                    and not discovery_report_is_stale(report)
                )
                if gateway_still_scanning:
                    messages.info(request, "Cancelling equipment discovery on the Gateway…")
                else:
                    # No active Gateway scan was reported. Release the local UI immediately;
                    # a fresh running report can still reattach on the next poll.
                    summary = dict(run.summary or {})
                    discovery_meta = dict(summary.get("discovery") or {})
                    discovery_meta["last_cancelled_scan_id"] = scan_id
                    discovery_meta["last_status"] = "cancelled"
                    discovery_meta.pop("active_scan_id", None)
                    discovery_meta.pop("command_id", None)
                    summary["discovery"] = discovery_meta
                    run.summary = summary
                    run.state = DeploymentSetupRun.State.CONFIGURING
                    run.save(update_fields=["summary", "state", "updated_at"])
                    messages.info(request, "Scan stopped in setup. You can retry when the equipment is ready.")
            except Exception as exc:
                messages.error(request, f"Discovery cancellation could not be sent: {exc}")
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        if action == "validate_selected" or action.startswith("save_and_validate:"):
            selected = (
                [action.partition(":")[2]]
                if action.startswith("save_and_validate:")
                else request.POST.getlist("device_index")
            )
            if not selected:
                messages.warning(request, "Review an equipment row and choose Test live readings.")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
            for raw_index in selected:
                try:
                    if str(raw_index).startswith("saved-"):
                        raise ValueError(
                            "This equipment was not seen in the latest scan. "
                            "Reconnect it and scan again before validation."
                        )
                    candidate, index, existing_item = _candidate_for_ref(
                        discovered_devices,
                        run,
                        raw_index,
                        request.POST.get(f"candidate_key_{raw_index}", ""),
                    )
                    template_id = request.POST.get(f"template_{raw_index}", "").strip()
                    template = visible_templates_for_team(request.team).filter(pk=template_id).first()
                    if not template:
                        raise ValueError("Choose an equipment template before validation.")
                    if not guided_capable and not template.is_verified:
                        messages.error(
                            request,
                            "Compatibility mode only permits Novena-verified templates. "
                            "Update the Gateway to validate private or AI draft templates.",
                        )
                        continue
                    connection, candidate = _candidate_connection_from_post(request.POST, candidate, raw_index)
                    candidate["customer_name"] = (
                        request.POST.get(f"name_{raw_index}", "").strip()
                        or candidate.get("customer_name")
                        or candidate.get("signature")
                        or template.name
                    )
                    candidate["customer_draft_saved"] = True
                    candidate["skipped_for_now"] = False
                    item = create_or_update_candidate_item(
                        run=run, index=index, candidate=candidate, existing_item=existing_item
                    )
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
                        request.POST.get(f"name_{raw_index}", "").strip()
                        or candidate.get("customer_name")
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
                        item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
                        item.state = DeploymentSetupItem.State.NEEDS_ATTENTION
                        item.validation_result = {
                            "status": "compatibility",
                            "message": (
                                "The Gateway needs an update before it can show live readings "
                                "for customer confirmation."
                            ),
                        }
                        item.save()
                        append_setup_event(
                            run,
                            "legacy_template_saved",
                            "A template was saved, but live readings need a Gateway update before confirmation.",
                            item=item,
                            actor=request.user,
                        )
                    request.session["onboarding_device_id"] = device.pk
                except (IndexError, TypeError, ValueError) as exc:
                    messages.error(request, f"One equipment candidate could not be prepared: {exc}")
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        if action == "manual":
            if not guided_capable:
                if not gateway.remote_control_clock_ready:
                    messages.warning(
                        request,
                        "The Gateway is synchronizing its secure clock. Your saved work is safe; "
                        "live validation will unlock automatically when it is ready.",
                    )
                else:
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
                discovered_candidate, draft_index, draft_item = (
                    _candidate_for_ref(
                        discovered_devices,
                        run,
                        raw_draft_index,
                        request.POST.get("draft_candidate_key", ""),
                    )
                    if raw_draft_index
                    else ({}, None, None)
                )
            except (IndexError, TypeError, ValueError):
                draft_index = None
                draft_item = None
                discovered_candidate = {}
            candidate_data = {
                **discovered_candidate,
                "signature": discovered_candidate.get("signature") or name,
                "customer_name": name,
                "connection": protocol,
                "interface": port_key,
                **(
                    {"host": connection.get("host"), "port": connection.get("port")} if protocol == "modbus_tcp" else {}
                ),
                "slave_id": connection.get("slave_id"),
            }
            if draft_index is not None or draft_item is not None:
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
            raw_draft_index = request.POST.get("draft_index", "").strip()
            try:
                requested_candidate, draft_index, draft_item = (
                    _candidate_for_ref(
                        discovered_devices,
                        run,
                        raw_draft_index,
                        request.POST.get("draft_candidate_key", ""),
                    )
                    if raw_draft_index
                    else ({}, None, None)
                )
            except (IndexError, TypeError, ValueError) as exc:
                messages.error(request, f"Review this equipment again before requesting a template: {exc}")
                return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
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
            if raw_draft_index:
                try:
                    requested_candidate = dict(requested_candidate)
                    requested_candidate["customer_draft_saved"] = True
                    draft_item = create_or_update_candidate_item(
                        run=run,
                        index=draft_index,
                        candidate=requested_candidate,
                        existing_item=draft_item,
                    )
                except ValueError as exc:
                    equipment_request.delete()
                    messages.error(request, f"This template request could not be linked to the equipment: {exc}")
                    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)
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
            if raw_draft_index:
                return redirect(
                    f"{reverse('web_team:onboarding:step_3_discover', args=[team_slug])}"
                    f"?candidate={raw_draft_index}&workflow=request#template-request-panel-{raw_draft_index}"
                )
            return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)

        if action == "deploy":
            if not guided_capable:
                if not gateway.remote_control_clock_ready:
                    messages.error(
                        request,
                        "The Gateway is synchronizing its secure clock. Your configuration is saved; "
                        "deployment will unlock automatically when it is ready.",
                    )
                else:
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
    deployable_items = list(run.items.filter(
        state__in=[DeploymentSetupItem.State.VALIDATED, DeploymentSetupItem.State.TELEMETRY_CONFIRMED],
        device__isnull=False,
        removed_at__isnull=True,
    ).select_related("device"))
    can_deploy = bool(deployable_items)
    commissioning = build_commissioning_context(request.team, gateway=gateway, session=request.session, setup_run=run)
    _attach_equipment_readings(commissioning)
    equipment_progress = _equipment_progress(run, commissioning, scan_state, can_deploy)
    template_workflow = request.GET.get("workflow", "")
    if template_workflow not in {"custom", "request"}:
        template_workflow = ""
    active_candidate_ref = request.GET.get("candidate", "")
    active_candidate = next(
        (
            candidate
            for candidate in commissioning["device_candidates"]
            if candidate["action_ref"] == active_candidate_ref
            or (
                active_candidate_ref.startswith("saved-")
                and str(candidate.get("setup_item_id")) == active_candidate_ref.removeprefix("saved-")
            )
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
        "guided_setup_available": guided_capable,
        "scan_state": scan_state,
        "can_deploy": can_deploy,
        "deployable_items": deployable_items,
        "commissioning": commissioning,
        "equipment_progress": equipment_progress,
        "template_workflow": template_workflow if active_candidate else "",
        "active_candidate": active_candidate,
    }
    return render(request, "onboarding/step_3_discover.html", context)


@require_permission("view_devices")
def discovery_poll(request, team_slug):
    """HTMX endpoint: refresh scan status and equipment rows together."""
    from apps.devices.deployment_setup import TERMINAL_RUN_STATES
    from apps.devices.gateway_config_delivery import gateway_supports_guided_setup

    gateway_id = request.session.get("onboarding_gateway_id")
    if not gateway_id:
        return render(request, "onboarding/partials/discovery_devices.html", {"discovered_devices": []})
    gateway = Gateway.objects.filter(id=gateway_id, team=request.team).first()
    discovered_devices = (gateway.discovery_data or {}).get("devices", []) if gateway else []
    profile = get_site_profile(gateway.site) if gateway else profile_for_request(request)
    templates = rank_templates_for_profile(visible_templates_for_team(request.team), profile)
    runs = DeploymentSetupRun.objects.filter(team=request.team, gateway=gateway) if gateway else None
    setup_run = (
        runs.exclude(state__in=TERMINAL_RUN_STATES).order_by("-created_at").first()
        or runs.order_by("-created_at").first()
        if runs is not None
        else None
    )
    if setup_run:
        from apps.devices.deployment_setup import discovery_scan_state, sync_setup_run

        setup_run = _reattach_running_discovery(setup_run)
        setup_run = sync_setup_run(setup_run)
        _require_customer_confirmation_for_predeployment(setup_run)
        scan_state = discovery_scan_state(setup_run)
    else:
        scan_state = {
            "key": "idle",
            "title": "Ready to scan",
            "message": "Connect your equipment to the Gateway, then start a scan.",
        }
    deployable_items = list(
        setup_run.items.filter(
            state__in=[DeploymentSetupItem.State.VALIDATED, DeploymentSetupItem.State.TELEMETRY_CONFIRMED],
            device__isnull=False,
            removed_at__isnull=True,
        ).select_related("device")
    ) if setup_run else []
    can_deploy = bool(deployable_items)
    commissioning = build_commissioning_context(
        request.team, gateway=gateway, session=request.session, setup_run=setup_run
    )
    _attach_equipment_readings(commissioning)
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
            "guided_setup_available": bool(gateway and gateway_supports_guided_setup(gateway)),
            "include_scan_oob": True,
            "can_deploy": can_deploy,
            "deployable_items": deployable_items,
            "commissioning": commissioning,
            "equipment_progress": _equipment_progress(setup_run, commissioning, scan_state, can_deploy)
            if setup_run
            else None,
        },
    )


@require_permission("manage_devices")
def step_3_device(request, team_slug):
    """Compatibility redirect: equipment setup is consolidated in Guided Setup."""
    messages.info(request, "Equipment templates and manual setup are now available in Guided Setup.")
    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)


def _deployment_run_for_device(team, device):
    """Keep Verify and Finish tied to the configuration sent for this device."""
    runs = DeploymentSetupRun.objects.filter(team=team, gateway=device.gateway)
    return (
        runs.filter(items__device=device, configurations__isnull=False).order_by("-created_at").first()
        or runs.filter(items__device=device).order_by("-created_at").first()
    )


@require_permission("manage_devices")
def step_4_alert(request, team_slug):
    device_id = request.session.get("onboarding_device_id")
    if not device_id:
        return redirect("web_team:onboarding:step_3_device", team_slug=team_slug)
    device = get_object_or_404(Device, id=device_id, team=request.team)

    profile = get_site_profile(device.site)
    from apps.devices.config_generator import human_config_preview
    from apps.devices.deployment_setup import deployment_progress, sync_setup_run, verification_progress

    setup_run = _deployment_run_for_device(request.team, device)
    if setup_run:
        setup_run = sync_setup_run(setup_run)

    if request.method == "POST":
        if setup_run and setup_run.state in {
            DeploymentSetupRun.State.COMPLETED,
            DeploymentSetupRun.State.COMPLETED_ATTENTION,
        }:
            return redirect("web_team:onboarding:complete", team_slug=team_slug)
        messages.info(request, "Novena is still verifying the Gateway and first live data.")
        return redirect("web_team:onboarding:step_4_alert", team_slug=team_slug)

    commissioning = build_commissioning_context(
        request.team, gateway=device.gateway, session=request.session, setup_run=setup_run
    )

    context = {
        "steps": ONBOARDING_STEPS,
        "current_step": 4,
        "device": device,
        "profile": profile,
        "setup_run": setup_run,
        "deployment_progress": deployment_progress(setup_run) if setup_run else [],
        "verification_progress": verification_progress(setup_run) if setup_run else [],
        "config_preview": human_config_preview(device.gateway),
        "commissioning": commissioning,
    }
    return render(request, "onboarding/step_4_alert.html", context)


@require_permission("manage_devices")
def complete(request, team_slug):
    site_id = request.session.get("onboarding_site_id")
    gateway_id = request.session.get("onboarding_gateway_id")
    device_id = request.session.get("onboarding_device_id")
    gateway = Gateway.objects.filter(id=gateway_id, team=request.team).first() if gateway_id else None
    device = Device.objects.filter(id=device_id, team=request.team).first() if device_id else None
    setup_run = _deployment_run_for_device(request.team, device) if device else None
    commissioning = build_commissioning_context(
        request.team, gateway=gateway, session=request.session, setup_run=setup_run
    )
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
