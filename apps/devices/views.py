import csv
import json
import logging
import re
import uuid
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from apps.teams.decorators import require_permission
from apps.teams.mixins import PermissionRequiredMixin

from .models import (
    ControlActivation,
    Device,
    DeviceTemplate,
    Gateway,
    GatewayConfig,
    RemoteCommand,
    RemoteCommandApproval,
    RpcCommand,
    Site,
)
from .services import gateways_for_team, sites_for_team, visible_templates_for_team
from .tasks import generate_template_ai_task
from .template_ai import save_approved_template

logger = logging.getLogger("novena_hub")


@require_permission("view_command_audit")
def gateway_control_center(request, team_slug, pk):
    from .operator_copy import (
        event_label,
        operation_label,
        readiness_blocker_label,
        readiness_state_label,
        status_label,
    )

    gateway = get_object_or_404(Gateway, pk=pk, team=request.team)
    assessment = gateway.control_readiness_assessments.order_by("-assessed_at").first()
    if assessment:
        assessment.operator_state = readiness_state_label(assessment.state)
        assessment.operator_blockers = [readiness_blocker_label(item) for item in assessment.blockers]
    commands = list(RemoteCommand.objects.filter(team=request.team, gateway=gateway).prefetch_related("events")[:100])
    for command in commands:
        command.operator_operation = operation_label(command.operation)
        command.operator_status = status_label(command.status)
        command.operator_events = [
            {
                "happened_at": event.happened_at,
                "event": event_label(event.event_type),
                "status": status_label(event.to_status) if event.to_status else "Recorded",
            }
            for event in command.events.all()
        ]
    return render(
        request,
        "devices/control_center.html",
        {
            "gateway": gateway,
            "assessment": assessment,
            "activations": ControlActivation.objects.filter(
                team=request.team,
                device__gateway=gateway,
            ).select_related("device"),
            "approvals": RemoteCommandApproval.objects.filter(
                command__team=request.team,
                command__gateway=gateway,
                status=RemoteCommandApproval.Status.PENDING,
            ).select_related("command", "command__device"),
            "commands": commands,
        },
    )


@require_POST
@require_permission("toggle_remote_control")
def gateway_emergency_disable(request, team_slug, pk):
    gateway = get_object_or_404(Gateway, pk=pk, team=request.team)
    from .control_readiness import emergency_disable

    epoch = emergency_disable(
        team=request.team,
        actor=request.user,
        reason=request.POST.get("reason", "Emergency disable from control center"),
        gateway=gateway,
    )
    return JsonResponse(
        {
            "status": "disabled_at_hub",
            "control_epoch": epoch,
            "gateway_acknowledged": False,
            "message": (
                "New remote-control requests are blocked. The gateway has not yet confirmed the change, "
                "so verify the equipment locally before leaving the site."
            ),
        }
    )


@require_POST
@require_permission("approve_high_risk_commands")
def remote_command_approve(request, team_slug, command_id):
    command = get_object_or_404(RemoteCommand, pk=command_id, team=request.team)
    recent_value = request.session.get("remote_control_recent_auth_at")
    recent_auth_at = datetime.fromisoformat(recent_value) if recent_value else None
    mfa_value = request.session.get("remote_control_mfa_verified_at")
    mfa_verified = bool(mfa_value)
    from .control_readiness import ReadinessDenied, approve_command

    try:
        approve_command(
            command=command,
            approver=request.user,
            mfa_verified=mfa_verified,
            recent_auth_at=recent_auth_at,
            reason=request.POST.get("reason", ""),
        )
    except ReadinessDenied as exc:
        return JsonResponse({"error": str(exc), "code": exc.code}, status=409)
    return JsonResponse({"status": "queued_for_dispatch", "command_id": str(command.pk)})


@require_permission("export_command_audit")
def command_audit_export(request, format):
    commands = RemoteCommand.objects.filter(team=request.team).select_related(
        "gateway",
        "device",
        "requested_by",
    )
    records = [
        {
            "command_id": str(command.pk),
            "requested_at": command.created_at.isoformat(),
            "requester": command.actor_snapshot.get("email", ""),
            "gateway": command.gateway.serial_number,
            "device": command.device.name if command.device_id else "",
            "operation": command.operation,
            "command_key": command.command_key,
            "requested_value": command.requested_value,
            "status": command.status,
            "error_code": command.error_code,
            "error_message": command.error_message,
        }
        for command in commands
    ]
    if format == "json":
        return JsonResponse({"commands": records})
    if format != "csv":
        raise Http404
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="remote-command-audit.csv"'
    fields = (
        list(records[0])
        if records
        else [
            "command_id",
            "requested_at",
            "requester",
            "gateway",
            "device",
            "operation",
            "command_key",
            "requested_value",
            "status",
            "error_code",
            "error_message",
        ]
    )
    writer = csv.DictWriter(response, fieldnames=fields)
    writer.writeheader()
    writer.writerows(records)
    return response


class SiteListView(PermissionRequiredMixin, ListView):
    permission_required = "view_devices"
    model = Site
    template_name = "devices/site_list.html"
    context_object_name = "sites"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "sites"
        return context


class SiteDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "view_devices"
    model = Site
    template_name = "devices/site_detail.html"
    context_object_name = "site"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "sites"
        from apps.alerts.models import Alert
        from apps.telemetry.anomaly import get_ai_insights
        from apps.telemetry.services import get_site_summary_stats

        context["stats"] = get_site_summary_stats(self.object)

        # Process discovery and conflicts for UI
        for gateway in self.object.gateways.all():
            port_map = {}

            # 1. Map registered devices by their port (interface key)
            for device in gateway.devices.all():
                if device.port:
                    port_map[device.port] = {"status": "registered", "device": device}

            # 2. Layer discovery data — use interface name as port key
            discovery_data = gateway.discovery_data or {}
            discovery_list = discovery_data.get("devices", [])
            interfaces = discovery_data.get("interfaces", [])

            for disc in discovery_list:
                # Use interface as the port key (e.g., "/dev/ttyUSB0", "192.168.1.100:502")
                port_key = disc.get("interface") or disc.get("port")
                if not port_key:
                    continue
                port_key = str(port_key)

                if port_key in port_map:
                    # Check for conflict
                    reg_device = port_map[port_key]["device"]
                    sig = disc.get("signature", "")
                    if (
                        sig
                        and sig.lower() not in reg_device.name.lower()
                        and sig.lower() not in reg_device.device_type.lower()
                    ):
                        port_map[port_key]["status"] = "conflict"
                        port_map[port_key]["discovered"] = disc
                else:
                    port_map[port_key] = {"status": "discovered", "discovered": disc}

            # 3. Add empty slots for interfaces with no devices/discoveries
            for iface in interfaces:
                iface_name = iface.get("name", "")
                if iface_name and iface_name not in port_map:
                    port_map[iface_name] = {"status": "empty", "interface": iface}

            # Attach to the gateway object for use in templates
            gateway.computed_port_map = port_map
            gateway.computed_interfaces = interfaces

        # Aggregate insights for all devices in the site
        site_insights = []
        for device in self.object.devices.all():
            site_insights.extend(get_ai_insights(device))
        context["ai_insights"] = site_insights[:3]  # Show top 3

        context["recent_alerts"] = Alert.objects.filter(device__site=self.object, status="active").order_by(
            "-triggered_at"
        )[:5]
        return context


class SiteCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_devices"
    model = Site
    fields = ["name", "solution_profile", "site_type", "address", "latitude", "longitude", "timezone"]
    template_name = "devices/site_form.html"

    def form_valid(self, form):
        form.instance.team = self.request.team
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("web_team:devices:site_list", args=[self.request.team.slug])


class SiteUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_devices"
    model = Site
    fields = ["name", "solution_profile", "site_type", "address", "latitude", "longitude", "timezone"]
    template_name = "devices/site_form.html"

    def get_success_url(self):
        return reverse_lazy("web_team:devices:site_list", args=[self.request.team.slug])


class SiteDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = "manage_devices"
    model = Site
    template_name = "devices/site_confirm_delete.html"

    def form_valid(self, form):
        confirmation_name = self.request.POST.get("confirmation_name", "").strip()
        expected_name = self.object.name.strip()

        if confirmation_name != expected_name:
            form.add_error(None, "Type the site name exactly to confirm deletion.")
            return self.form_invalid(form)

        if self.object.gateways.exists():
            form.add_error(
                None,
                "This site still has Gateway records. Move active Gateways to another site or securely release them "
                "first. Contact Novena support to remove archived Gateway records.",
            )
            return self.form_invalid(form)

        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("web_team:devices:site_list", args=[self.request.team.slug])


class GatewayListView(PermissionRequiredMixin, ListView):
    permission_required = "view_devices"
    model = Gateway
    template_name = "devices/gateway_list.html"
    context_object_name = "gateways"

    def get_queryset(self):
        return super().get_queryset().exclude(lifecycle_status="released")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "gateways"
        return context


class GatewayDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "view_devices"
    model = Gateway
    template_name = "devices/gateway_detail.html"
    context_object_name = "gateway"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.telemetry.models import GatewayLog

        from .models import FirmwareRelease

        gateway = self.object
        context["release_request"] = gateway.release_requests.first()
        context["latest_activation"] = gateway.activations.first()
        context["recent_logs"] = GatewayLog.objects.filter(gateway=gateway)[:20]
        context["recent_rpc"] = RpcCommand.objects.filter(gateway=gateway)[:10]
        context["recent_configs"] = GatewayConfig.objects.filter(gateway=gateway)[:5]

        latest_release = FirmwareRelease.objects.filter(is_active=True).first()
        if latest_release and latest_release.version != gateway.firmware_version:
            context["update_available"] = True
            context["latest_release"] = latest_release
            context["ota_signing_ready"] = latest_release.is_signed or bool(
                getattr(settings, "NOVENA_OTA_SIGNING_PRIVATE_KEY", "")
            )

        return context


class GatewayCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_devices"
    model = Gateway
    fields = ["site", "name", "serial_number"]
    template_name = "devices/gateway_form.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["site"].queryset = sites_for_team(self.request.team)
        return form

    def post(self, request, *args, **kwargs):
        from django.shortcuts import redirect

        from .services import GatewayClaimError, claim_gateway_for_team

        self.object = None
        form = self.get_form()
        claim_code = request.POST.get("claim_code", "").strip().upper()

        if not claim_code:
            form.add_error(None, "Claim code is required. Check the sticker on your gateway.")
            return self.form_invalid(form)

        if not form.is_valid():
            return self.form_invalid(form)

        site = form.cleaned_data["site"]
        if site.team != request.team:
            form.add_error("site", "Select a site that belongs to the current team.")
            return self.form_invalid(form)

        try:
            self.object = claim_gateway_for_team(
                request.team,
                site,
                form.cleaned_data["name"],
                form.cleaned_data["serial_number"],
                claim_code,
            )
        except GatewayClaimError as e:
            form.add_error(None, str(e))
            return self.form_invalid(form)

        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse_lazy("web_team:devices:gateway_detail", args=[self.request.team.slug, self.object.pk])


class GatewayUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_devices"
    model = Gateway
    # The appliance identity and observed connectivity state are managed by the
    # claim/heartbeat flows. Allowing either to be edited here can strand a
    # gateway under an identity that its MQTT credentials do not own or display
    # a customer-selected status that contradicts the latest heartbeat.
    fields = ["site", "name"]
    template_name = "devices/gateway_form.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["site"].queryset = sites_for_team(self.request.team)
        return form

    def get_success_url(self):
        return reverse_lazy("web_team:devices:gateway_list", args=[self.request.team.slug])


class GatewayDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = "manage_devices"
    model = Gateway
    template_name = "devices/gateway_confirm_delete.html"

    def form_valid(self, form):
        confirmation_serial = self.request.POST.get("confirmation_serial", "").strip().upper()
        expected_serial = self.object.serial_number.strip().upper()

        if confirmation_serial != expected_serial:
            form.add_error(None, "Type the gateway serial number exactly to confirm deletion.")
            return self.form_invalid(form)

        from .gateway_release import request_gateway_release

        request_gateway_release(self.object, requested_by=self.request.user)
        return HttpResponseRedirect(
            reverse_lazy("web_team:devices:gateway_detail", args=[self.request.team.slug, self.object.pk])
        )

    def get_success_url(self):
        return reverse_lazy("web_team:devices:gateway_list", args=[self.request.team.slug])


# ── Gateway Management Views (Cloud ↔ Edge) ────────────────────────────


@require_permission("manage_devices")
@require_POST
def gateway_retry_activation(request, team_slug, pk):
    from .activation import reissue_activation_for_gateway

    gateway = Gateway.objects.get(pk=pk, team=request.team)
    try:
        reissue_activation_for_gateway(gateway, force=True)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.info(request, "Novena is securely retrying Gateway activation.")
    return HttpResponseRedirect(reverse_lazy("web_team:devices:gateway_detail", args=[team_slug, pk]))


@require_permission("manage_devices")
@require_POST
def gateway_rotate_password(request, team_slug, pk):
    """Rotate the MQTT password for a gateway."""
    import secrets

    from apps.telemetry.mqtt_publisher import publish_credential_rotation

    from .mqtt_provisioning import rotate_gateway_password

    gateway = Gateway.objects.get(pk=pk, team=request.team)

    if gateway.lifecycle_status in {"release_pending", "released"}:
        return JsonResponse(
            {"code": "gateway_quarantined", "error": "This Gateway is being securely released."},
            status=409,
        )

    if gateway.status != "online":
        return JsonResponse({"error": "Gateway must be online to rotate password."}, status=400)

    new_password = secrets.token_urlsafe(32)

    # 1. Update Mosquitto dynsec client password
    rotate_gateway_password(gateway, new_password)

    # 2. Publish new credentials to Edge via provision topic
    publish_credential_rotation(gateway, new_password)

    # 3. Update Cloud DB
    gateway.mqtt_password = make_password(new_password)
    gateway.credential_rotation_status = "pending"
    gateway.save(update_fields=["mqtt_password", "credential_rotation_status"])

    logger.info("Password rotated for gateway %s", gateway.serial_number)

    # Return HTMX-friendly response
    if request.headers.get("HX-Request"):
        return HttpResponse(
            '<div class="flex items-center gap-2 text-sm text-green-600 dark:text-green-400 font-medium">'
            '<i class="fa fa-check-circle"></i> Password rotated successfully. Gateway will reconnect shortly.'
            "</div>"
        )
    return JsonResponse({"status": "rotated", "gateway": gateway.serial_number})


@require_permission("request_low_risk_commands")
@require_POST
def gateway_send_rpc(request, team_slug, pk):
    """Send an RPC command to a gateway from the dashboard."""
    from .remote_control import CommandDenied, request_remote_command

    gateway = Gateway.objects.get(pk=pk, team=request.team)
    method = request.POST.get("method")
    if method == "update_firmware":
        return JsonResponse(
            {"error": "Firmware updates must use the signed OTA release endpoint."},
            status=400,
        )
    try:
        params = json.loads(request.POST.get("params", "{}"))
        command = request_remote_command(
            gateway=gateway,
            operation=method,
            requested_by=request.user,
            params=params,
            reason=request.POST.get("reason", ""),
        )
    except (CommandDenied, json.JSONDecodeError) as exc:
        from .operator_copy import customer_safe_control_error

        return JsonResponse(
            {
                "error": customer_safe_control_error(getattr(exc, "code", "invalid_request")),
                "code": getattr(exc, "code", "invalid_request"),
            },
            status=403 if getattr(exc, "code", "") == "permission_denied" else 400,
        )

    return JsonResponse({"command_id": str(command.pk), "method": method, "status": command.status})


@require_permission("manage_devices")
@require_POST
def gateway_push_config(request, team_slug, pk):
    """Push a config update to a gateway."""
    from .gateway_config_delivery import GatewayConfigUnsupported, queue_gateway_config

    gateway = Gateway.objects.get(pk=pk, team=request.team)
    action = request.POST.get("action", "full_update")
    if action not in {"full_update", "connector_update", "connector_add", "connector_remove"}:
        return JsonResponse({"code": "invalid_action", "message": "Choose a supported settings action."}, status=400)
    try:
        config = json.loads(request.POST.get("config", ""))
    except (TypeError, json.JSONDecodeError):
        return JsonResponse({"code": "invalid_config", "message": "Settings must be valid JSON."}, status=400)
    if not isinstance(config, dict) or not config:
        return JsonResponse({"code": "invalid_config", "message": "Settings must be a non-empty object."}, status=400)

    try:
        config_record = queue_gateway_config(gateway, action, config)
    except GatewayConfigUnsupported as exc:
        return JsonResponse({"code": "gateway_update_required", "message": str(exc)}, status=409)
    except ValueError as exc:
        return JsonResponse({"code": "invalid_config", "message": str(exc)}, status=400)

    gateway.lifecycle_status = "commissioning"
    gateway.save(update_fields=["lifecycle_status"])

    return JsonResponse(
        {
            "request_id": str(config_record.request_id),
            "revision": config_record.revision,
            "action": action,
            "status": config_record.status,
        },
        status=202,
    )


@require_permission("manage_devices")
@require_POST
def gateway_retry_config(request, team_slug, pk, config_pk):
    from .gateway_config_delivery import GatewayConfigUnsupported, retry_gateway_config

    config_record = GatewayConfig.objects.select_related("gateway").get(
        pk=config_pk,
        gateway_id=pk,
        team=request.team,
    )
    try:
        replacement = retry_gateway_config(config_record)
    except GatewayConfigUnsupported as exc:
        messages.error(request, str(exc))
    except ValueError as exc:
        messages.info(request, str(exc))
    else:
        messages.info(request, f"Settings revision {replacement.revision} is queued for secure delivery.")
    return HttpResponseRedirect(reverse_lazy("web_team:devices:gateway_detail", args=[team_slug, pk]))


@require_permission("view_devices")
def gateway_logs(request, team_slug, pk):
    """View gateway logs with optional level filter."""
    from apps.telemetry.models import GatewayLog

    gateway = Gateway.objects.get(pk=pk, team=request.team)
    level = request.GET.get("level")

    logs = GatewayLog.objects.filter(gateway=gateway)
    if level:
        logs = logs.filter(level=level)
    logs = logs[:200]

    return render(
        request,
        "devices/gateway_logs.html",
        {"gateway": gateway, "logs": logs, "current_level": level},
    )


@require_permission("view_devices")
def gateway_rpc_history(request, team_slug, pk):
    """View RPC command history for a gateway."""
    gateway = Gateway.objects.get(pk=pk, team=request.team)
    commands = RpcCommand.objects.filter(gateway=gateway)[:50]

    return render(
        request,
        "devices/gateway_rpc_history.html",
        {"gateway": gateway, "commands": commands},
    )


@require_permission("request_low_risk_commands")
@require_POST
def device_rpc_command(request, team_slug, gateway_pk, device_pk):
    """Compatibility endpoint: routes customer device RPC through DeviceCommand audit."""
    from .remote_control_protocol import canonical_device_operation
    from .services import send_device_command

    try:
        operation = canonical_device_operation(request.POST.get("method"))
        params = json.loads(request.POST.get("params", "{}"))
        if not isinstance(params, dict):
            raise ValueError("Command params must be an object.")
        if set(params) - {"command_key", "value"}:
            raise ValueError("Raw register parameters are not accepted.")
        key = params.get("command_key")
        if not key:
            raise ValueError("A mapped canonical device command key is required.")
        command_type = "write" if operation == "write_device" else "read"
        gateway = Gateway.objects.get(pk=gateway_pk, team=request.team)
        device = Device.objects.get(pk=device_pk, gateway=gateway)
        command = send_device_command(
            device,
            request.user,
            key,
            params.get("value"),
            command_type=command_type,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        from .operator_copy import customer_safe_control_error

        return JsonResponse(
            {
                "error": customer_safe_control_error(getattr(exc, "code", "invalid_request")),
                "code": getattr(exc, "code", "invalid_request"),
            },
            status=400,
        )

    return JsonResponse(
        {
            "request_id": str(command.rpc_command.request_id) if command.rpc_command else None,
            "transaction_id": str(command.transaction_id),
            "method": operation,
            "device": device.name,
            "status": command.status,
        }
    )


@require_permission("view_devices")
def device_rpc_status(request, team_slug, gateway_pk, device_pk, request_id):
    """Poll the status of an RPC command sent to a device."""
    rpc = RpcCommand.objects.filter(
        gateway__pk=gateway_pk,
        gateway__team=request.team,
        request_id=request_id,
    ).first()

    if not rpc:
        return JsonResponse(
            {"status": "not_found", "result": None, "error": "This equipment request could not be found."},
            status=404,
        )

    return JsonResponse(
        {
            "status": rpc.status,
            "result": rpc.result,
            "error": rpc.error_message or None,
        }
    )


@require_permission("view_devices")
def gateway_rpc_status(request, team_slug, gateway_pk, request_id):
    """Poll the status of an RPC command sent to a gateway."""
    rpc = RpcCommand.objects.filter(
        gateway__pk=gateway_pk,
        gateway__team=request.team,
        request_id=request_id,
    ).first()

    if not rpc:
        return JsonResponse(
            {"status": "not_found", "result": None, "error": "This gateway request could not be found."},
            status=404,
        )

    return JsonResponse(
        {
            "status": rpc.status,
            "result": rpc.result,
            "error": rpc.error_message or None,
        }
    )


class DeviceListView(PermissionRequiredMixin, ListView):
    permission_required = "view_devices"
    model = Device
    template_name = "devices/device_list.html"
    context_object_name = "devices"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "devices"
        return context


class DeviceDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "view_devices"
    model = Device
    template_name = "devices/device_detail.html"
    context_object_name = "device"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.dashboard.services import build_device_dashboard_context
        from apps.subscriptions.enforcement import get_latency_limit_for_team
        from apps.telemetry.anomaly import get_ai_insights

        dashboard_context = build_device_dashboard_context(self.object)
        context.update(dashboard_context)
        context["ai_insights"] = get_ai_insights(self.object)
        context["recent_commands"] = self.object.commands.all().order_by("-requested_at")[:10]
        context["remote_control_enabled"] = (
            self.request.team.remote_control_mode == self.request.team.RemoteControlMode.CONTROLLED
        )

        latency_limit_seconds = get_latency_limit_for_team(self.object.team)
        context["telemetry_fallback_interval_ms"] = int(max(5.0, latency_limit_seconds) * 1000)

        # Backward compatibility for existing control JavaScript/template code.
        context["writable_keys"] = [
            {"key": reg["key"], "label": reg["label"], "type": reg["config"].get("control", "toggle")}
            for reg in context["writable_registers"]
        ]

        if self.object.gateway_id:
            context["command_url"] = reverse_lazy(
                "web_team:devices:device_send_command",
                args=[self.request.team.slug, self.object.pk],
            )
            context["rpc_url"] = reverse_lazy(
                "web_team:devices:device_rpc_command",
                args=[self.request.team.slug, self.object.gateway_id, self.object.pk],
            )
        else:
            context["command_url"] = ""
            context["rpc_url"] = ""

        return context


# ... existing views ...


@require_permission("request_low_risk_commands")
def device_send_command(request, team_slug, pk):
    from django.shortcuts import get_object_or_404

    from .remote_control_protocol import canonical_device_operation

    try:
        submitted_method = request.POST.get("method") or request.POST.get("command_type") or request.POST.get("type")
        operation = canonical_device_operation(submitted_method)
        command_type = "write" if operation == "write_device" else "read"
        key = request.POST.get("key") or request.POST.get("command_key")
        raw_value = request.POST.get("value")
        if request.POST.get("params"):
            submitted = json.loads(request.POST.get("params", "{}"))
            if not isinstance(submitted, dict) or set(submitted) - {"command_key", "value"}:
                raise ValueError("Raw register parameters are not accepted.")
            key = submitted.get("command_key", key)
            raw_value = submitted.get("value", raw_value)
        if not key:
            raise ValueError("A mapped canonical device command key is required.")
        device = get_object_or_404(Device, pk=pk, team=request.team)
        from .services import send_device_command

        command = send_device_command(
            device,
            request.user,
            key,
            raw_value,
            command_type=command_type,
        )
        if request.headers.get("HX-Request"):
            return render(request, "devices/partials/command_status_badge.html", {"command": command})
        return JsonResponse(
            {
                "transaction_id": str(command.transaction_id),
                "status": command.status,
                "command_type": command.command_type,
                "command_key": command.command_key,
                "rpc_request_id": str(command.rpc_command.request_id) if command.rpc_command else None,
            }
        )
    except Exception as e:
        from django.utils.html import escape

        from .operator_copy import customer_safe_device_error

        safe_error = customer_safe_device_error(str(e), code=getattr(e, "code", ""))
        if request.headers.get("HX-Request"):
            return HttpResponse(f'<span class="text-error text-xs font-bold">{escape(safe_error)}</span>', status=400)
        return JsonResponse({"error": safe_error}, status=400)


@require_permission("view_devices")
def device_command_status(request, team_slug, pk, tx_id):
    from django.shortcuts import get_object_or_404

    from .models import DeviceCommand

    command = get_object_or_404(DeviceCommand, transaction_id=tx_id, team=request.team)
    if request.GET.get("format") == "json" or "application/json" in request.headers.get("Accept", ""):
        from .operator_copy import customer_safe_device_error

        result = command.response_payload.get("result") if isinstance(command.response_payload, dict) else None
        return JsonResponse(
            {
                "transaction_id": str(command.transaction_id),
                "status": command.status,
                "command_type": command.command_type,
                "command_key": command.command_key,
                "result": result,
                "response_payload": command.response_payload,
                "error": customer_safe_device_error(command.error_message) if command.error_message else None,
            }
        )
    return render(request, "devices/partials/command_status_badge.html", {"command": command})


class DeviceCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_devices"
    model = Device
    fields = ["gateway", "site", "template", "name", "device_type", "protocol", "energy_category", "connection_config"]
    template_name = "devices/device_form.html"

    def dispatch(self, request, *args, **kwargs):
        # Enforce device limits
        from apps.subscriptions.enforcement import can_add_device, get_device_limit_for_team

        if not can_add_device(request.team):
            limit = get_device_limit_for_team(request.team)
            count = Device.objects.filter(team=request.team).count()
            return render(
                request,
                "devices/upgrade_required.html",
                {
                    "limit": limit,
                    "count": count,
                },
            )
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["gateway"].queryset = gateways_for_team(self.request.team)
        form.fields["site"].queryset = sites_for_team(self.request.team)
        form.fields["template"].queryset = visible_templates_for_team(self.request.team)
        return form

    def form_valid(self, form):
        gateway = form.cleaned_data.get("gateway")
        site = form.cleaned_data.get("site")
        if gateway and site and gateway.site_id != site.id:
            form.add_error("gateway", "Select a gateway that belongs to the selected site.")
            return self.form_invalid(form)
        form.instance.team = self.request.team
        response = super().form_valid(form)
        if self.object.template and self.object.template.mapping_strategy == "site_defined":
            from .datapoint_maps import ensure_device_datapoint_map

            ensure_device_datapoint_map(self.object)
        return response

    def get_success_url(self):
        if self.object.template and self.object.template.mapping_strategy == "site_defined":
            return reverse_lazy(
                "web_team:devices:device_datapoint_mapping",
                args=[self.request.team.slug, self.object.pk],
            )
        return reverse_lazy("web_team:devices:device_list", args=[self.request.team.slug])


class DeviceUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_devices"
    model = Device
    fields = ["name", "device_type", "protocol", "energy_category", "connection_config", "status"]
    template_name = "devices/device_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        connection_changed = "connection_config" in form.changed_data
        if connection_changed and self.object.template and self.object.template.mapping_strategy == "site_defined":
            from .datapoint_maps import ensure_device_datapoint_map

            mapping = ensure_device_datapoint_map(self.object)
            mapping.status = mapping.Status.DRAFT
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
            metadata = dict(self.object.metadata or {})
            metadata["guided_setup_validation"] = "pending"
            self.object.metadata = metadata
            self.object.save(update_fields=["metadata", "updated_at"])
        return response

    def get_success_url(self):
        return reverse_lazy("web_team:devices:device_list", args=[self.request.team.slug])


@require_permission("manage_devices")
def device_datapoint_mapping(request, team_slug, pk):
    """Edit, live-test, and explicitly confirm a programmable device's signals."""
    from .datapoint_maps import (
        clone_device_datapoint_map,
        confirm_device_datapoint_map,
        datapoints_from_csv,
        datapoints_to_csv,
        device_requires_mapping,
        ensure_device_datapoint_map,
        rollback_device_datapoint_map,
        save_device_datapoint_map,
    )
    from .deployment_setup import (
        append_setup_event,
        get_or_create_setup_run,
        start_validation,
        sync_setup_run,
    )
    from .models import DeploymentSetupItem, DeviceDatapointMap, DeviceDatapointMapRevision

    device = get_object_or_404(
        Device.objects.select_related("template", "gateway", "site"),
        pk=pk,
        team=request.team,
    )
    if not device.gateway or not device_requires_mapping(device):
        messages.info(request, "This equipment uses a fixed Novena signal map.")
        return redirect("web_team:devices:device_detail", team_slug=team_slug, pk=device.pk)

    mapping = ensure_device_datapoint_map(device)
    run = get_or_create_setup_run(team=request.team, gateway=device.gateway, initiated_by=request.user)
    item, _ = DeploymentSetupItem.objects.get_or_create(
        team=request.team,
        run=run,
        device=device,
        defaults={
            "selected_template": device.template,
            "connection": device.connection_config,
            "candidate_data": {
                "signature": device.name,
                "connection": device.protocol,
                "interface": device.port or "",
            },
            "state": DeploymentSetupItem.State.TEMPLATE_SELECTED,
        },
    )
    run = sync_setup_run(run)
    mapping.refresh_from_db()
    item.refresh_from_db()

    if request.method == "POST":
        action = request.POST.get("action", "save")
        try:
            if action == "export_csv":
                response = HttpResponse(
                    "\ufeff" + datapoints_to_csv(mapping.datapoints),
                    content_type="text/csv; charset=utf-8",
                )
                response["Content-Disposition"] = f'attachment; filename="device-{device.pk}-datapoints.csv"'
                return response
            if action == "import_csv":
                upload = request.FILES.get("csv_file")
                if not upload:
                    raise ValidationError("Choose a CSV file to import.")
                if upload.size > 256 * 1024:
                    raise ValidationError("The CSV file must be 256 KB or smaller.")
                try:
                    imported = datapoints_from_csv(upload.read().decode("utf-8-sig"))
                except UnicodeDecodeError as exc:
                    raise ValidationError("The CSV file must use UTF-8 encoding.") from exc
                mapping = save_device_datapoint_map(device=device, team=request.team, datapoints=imported)
                item.connection = device.connection_config
                item.datapoints = mapping.datapoints
                item.state = DeploymentSetupItem.State.TEMPLATE_SELECTED
                item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
                item.validation_result = {}
                item.save(
                    update_fields=[
                        "connection",
                        "datapoints",
                        "state",
                        "trust_level",
                        "validation_result",
                        "updated_at",
                    ]
                )
                messages.success(
                    request,
                    f"Imported {len(imported)} signals as a draft. Validate them before deployment.",
                )
            elif action == "save":
                raw_datapoints = json.loads(request.POST.get("datapoints_json", "[]"))
                connection = dict(device.connection_config or {})
                connection["slave_id"] = int(request.POST.get("slave_id", 1))
                connection["timeout"] = float(request.POST.get("timeout", 3))
                connection["byteOrder"] = request.POST.get("byte_order", "BIG")
                connection["wordOrder"] = request.POST.get("word_order", "BIG")
                connection["requested_polling_interval"] = float(request.POST.get("polling_interval", 5))
                if connection["byteOrder"] not in {"BIG", "LITTLE"} or connection["wordOrder"] not in {
                    "BIG",
                    "LITTLE",
                }:
                    raise ValidationError("Choose a valid byte and word order.")
                if not 1 <= connection["slave_id"] <= 247:
                    raise ValidationError("Slave ID must be between 1 and 247.")
                if not 1 <= connection["timeout"] <= 10:
                    raise ValidationError("Timeout must be between 1 and 10 seconds.")
                if not 1 <= connection["requested_polling_interval"] <= 3600:
                    raise ValidationError("Polling interval must be between 1 and 3600 seconds.")
                if device.protocol == "modbus_tcp":
                    import ipaddress

                    host = request.POST.get("host", "").strip()
                    ipaddress.ip_address(host)
                    connection["host"] = host
                    connection["port"] = int(request.POST.get("port", 502))
                    if not 1 <= connection["port"] <= 65535:
                        raise ValidationError("Port must be between 1 and 65535.")
                    device.port = f"{host}:{connection['port']}"
                else:
                    serial_port = request.POST.get("serial_port", "").strip()
                    if not serial_port:
                        raise ValidationError("Select the connected RS485 interface.")
                    connection["serial_port"] = serial_port
                    connection["baudrate"] = int(request.POST.get("baudrate", 9600))
                    connection["parity"] = request.POST.get("parity", "N")
                    connection["stopbits"] = int(request.POST.get("stopbits", 1))
                    if connection["parity"] not in {"N", "E", "O"}:
                        raise ValidationError("Choose a valid parity setting.")
                    device.port = serial_port
                device.connection_config = connection
                device.save(update_fields=["connection_config", "port", "updated_at"])
                mapping = save_device_datapoint_map(device=device, team=request.team, datapoints=raw_datapoints)
                item.connection = connection
                item.datapoints = mapping.datapoints
                item.state = DeploymentSetupItem.State.TEMPLATE_SELECTED
                item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
                item.validation_result = {}
                item.save(
                    update_fields=[
                        "connection",
                        "datapoints",
                        "state",
                        "trust_level",
                        "validation_result",
                        "updated_at",
                    ]
                )
                messages.success(request, "Signal mapping saved. Run a live validation before confirming it.")
            elif action in {"test_connection", "validate"}:
                start_validation(
                    item=item,
                    template=device.template,
                    requested_by=request.user,
                    connection_only=action == "test_connection",
                )
                messages.info(
                    request,
                    "The Gateway is checking the connection."
                    if action == "test_connection"
                    else "The Gateway is reading and decoding the mapped signals.",
                )
            elif action == "confirm":
                mapping = confirm_device_datapoint_map(
                    device=device,
                    team=request.team,
                    confirmed_by=request.user,
                )
                item.state = DeploymentSetupItem.State.VALIDATED
                item.trust_level = DeploymentSetupItem.Trust.CUSTOMER_VALIDATED
                item.datapoints = mapping.datapoints
                item.validation_result = mapping.last_validation
                item.save(update_fields=["state", "trust_level", "datapoints", "validation_result", "updated_at"])
                append_setup_event(
                    run,
                    "mapping_confirmed",
                    f"The live readings for {device.name} were explicitly confirmed.",
                    item=item,
                    actor=request.user,
                    evidence={"checksum": mapping.confirmed_checksum, "signal_count": len(mapping.datapoints)},
                )
                messages.success(request, "Live readings confirmed. This equipment is ready for deployment.")
            elif action == "clone":
                source = get_object_or_404(
                    Device.objects.select_related("template"),
                    pk=request.POST.get("source_device"),
                    team=request.team,
                    protocol=device.protocol,
                    datapoint_map__status=DeviceDatapointMap.Status.CONFIRMED,
                )
                mapping = clone_device_datapoint_map(source_device=source, target_device=device, team=request.team)
                item.connection = device.connection_config
                item.datapoints = mapping.datapoints
                item.state = DeploymentSetupItem.State.TEMPLATE_SELECTED
                item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
                item.validation_result = {}
                item.save(
                    update_fields=[
                        "connection",
                        "datapoints",
                        "state",
                        "trust_level",
                        "validation_result",
                        "updated_at",
                    ]
                )
                messages.success(request, "Mapping cloned. Validate it against this equipment before confirmation.")
            elif action == "rollback":
                revision = get_object_or_404(
                    DeviceDatapointMapRevision,
                    pk=request.POST.get("revision_id"),
                    team=request.team,
                    mapping=mapping,
                )
                mapping = rollback_device_datapoint_map(
                    device=device,
                    team=request.team,
                    revision=revision,
                )
                item.connection = device.connection_config
                item.datapoints = mapping.datapoints
                item.state = DeploymentSetupItem.State.TEMPLATE_SELECTED
                item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
                item.validation_result = {}
                item.save(
                    update_fields=[
                        "connection",
                        "datapoints",
                        "state",
                        "trust_level",
                        "validation_result",
                        "updated_at",
                    ]
                )
                append_setup_event(
                    run,
                    "mapping_revision_restored",
                    f"Revision {revision.revision_number} was restored as a draft for {device.name}.",
                    item=item,
                    actor=request.user,
                    evidence={"revision_id": revision.pk, "revision_number": revision.revision_number},
                )
                messages.success(
                    request,
                    f"Revision {revision.revision_number} restored as a draft. Validate it before deployment.",
                )
            else:
                raise ValidationError("Unknown mapping action.")
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            error_messages = exc.messages if isinstance(exc, ValidationError) and exc.messages else [str(exc)]
            for message in error_messages:
                messages.error(request, message)
        return redirect("web_team:devices:device_datapoint_mapping", team_slug=team_slug, pk=device.pk)

    clone_sources = (
        Device.objects.filter(
            team=request.team,
            protocol=device.protocol,
            template__mapping_strategy="site_defined",
            datapoint_map__status=DeviceDatapointMap.Status.CONFIRMED,
        )
        .exclude(pk=device.pk)
        .order_by("name")
    )
    return render(
        request,
        "devices/device_datapoint_mapping.html",
        {
            "device": device,
            "mapping": mapping,
            "setup_item": item,
            "setup_run": run,
            "clone_sources": clone_sources,
            "revisions": mapping.revisions.select_related("validated_by", "confirmed_by").all(),
            "connection": device.connection_config or {},
        },
    )


class DeviceDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = "manage_devices"
    model = Device
    template_name = "devices/device_confirm_delete.html"

    def get_success_url(self):
        return reverse_lazy("web_team:devices:device_list", args=[self.request.team.slug])


# HTMX Views


def _resolve_quick_add_gateway_and_site(team, gateway_id, site_id=None, lock_gateway=False):
    gateway_qs = gateways_for_team(team).select_related("site")
    if lock_gateway:
        gateway_qs = gateway_qs.select_for_update()

    gateway = get_object_or_404(gateway_qs, id=gateway_id)
    if site_id:
        site = get_object_or_404(sites_for_team(team), id=site_id)
        if gateway.site_id != site.id:
            raise Http404("Gateway does not belong to the selected site.")
        return gateway, site
    return gateway, gateway.site


def _discovery_entry_for_port(gateway, port):
    for discovered in (gateway.discovery_data or {}).get("devices", []):
        discovered_key = str(discovered.get("interface") or discovered.get("port", ""))
        if discovered_key == str(port):
            return discovered
    return None


def _first_readable_register(template):
    if not template.register_map:
        return None
    for key, config in template.register_map.items():
        if isinstance(config, dict) and not config.get("writable"):
            return {
                "key": key,
                "address": config.get("address", 0),
                "functionCode": config.get("functionCode", 3),
                "type": config.get("type", "uint16"),
                "unit": config.get("unit", ""),
                "objectsCount": config.get("objectsCount", 1),
            }
    return None


def _push_gateway_config_after_commit(gateway_id, team_id):
    try:
        from .config_generator import generate_and_push_config

        gateway = Gateway.objects.get(id=gateway_id, team_id=team_id)
        generate_and_push_config(gateway)
    except Exception as e:
        logger.warning("Config push failed after device creation: %s", e)


@require_permission("manage_devices")
def htmx_device_create(request, team_slug):
    gateway_id = request.GET.get("gateway_id")
    site_id = request.GET.get("site_id")
    port = request.GET.get("port")
    provision = request.GET.get("provision")
    resolve = request.GET.get("resolve")

    gateway, site = _resolve_quick_add_gateway_and_site(request.team, gateway_id, site_id)
    template_qs = visible_templates_for_team(request.team)

    prefill_data = {}
    discovery_entry = None
    suggested_templates = []
    if provision == "true" or resolve == "true":
        discovery_entry = _discovery_entry_for_port(gateway, port)
        if discovery_entry:
            prefill_data["name"] = discovery_entry.get("signature", "New Device")

            if discovery_entry.get("matched_template_id"):
                first_tpl = template_qs.filter(id=discovery_entry["matched_template_id"]).first()
                if first_tpl:
                    suggested_templates.append(first_tpl)

            sig = discovery_entry.get("signature", "").lower()
            if sig and sig != "unknown":
                other_tpls = template_qs.filter(Q(name__icontains=sig) | Q(manufacturer__icontains=sig)).exclude(
                    id__in=[template.id for template in suggested_templates]
                )[: 3 - len(suggested_templates)]
                suggested_templates.extend(other_tpls)

            if suggested_templates:
                prefill_data["template_id"] = suggested_templates[0].id

    if request.method == "POST":
        name = request.POST.get("name")
        template_id = request.POST.get("template_id")

        with transaction.atomic():
            gateway, site = _resolve_quick_add_gateway_and_site(request.team, gateway_id, site_id, lock_gateway=True)
            if provision == "true" or resolve == "true":
                discovery_entry = _discovery_entry_for_port(gateway, port)
            template = get_object_or_404(visible_templates_for_team(request.team), id=template_id)

            disc_meta = {}
            if discovery_entry:
                disc_meta = {
                    "interface": discovery_entry.get("interface"),
                    "connection": discovery_entry.get("connection"),
                    "slave_id": discovery_entry.get("slave_id"),
                    "baud_rate": discovery_entry.get("baud_rate"),
                    "signature": discovery_entry.get("signature"),
                    "identification": discovery_entry.get("identification"),
                }

            replacement = None
            if resolve == "true":
                replacement = Device.objects.filter(
                    team=request.team,
                    site=site,
                    gateway=gateway,
                    port=port,
                ).first()

            if not replacement:
                from apps.subscriptions.enforcement import can_add_device, get_device_limit_for_team

                if not can_add_device(request.team):
                    limit = get_device_limit_for_team(request.team)
                    count = Device.objects.filter(team=request.team).count()
                    return render(
                        request,
                        "devices/upgrade_required.html",
                        {"limit": limit, "count": count},
                    )

            if replacement:
                replacement.delete()

            from .deployment_setup import connection_from_candidate

            connection_config = connection_from_candidate(discovery_entry) if discovery_entry else {}
            device = Device.objects.create(
                team=request.team,
                site=site,
                gateway=gateway,
                port=port,
                name=name,
                template=template,
                device_type=template.device_type,
                protocol=template.protocol,
                connection_config=connection_config,
                discovery_meta=disc_meta,
            )

            from apps.events.services import log_event

            log_event(
                category="audit",
                message=f"Created device {device.name} via Intelligent Port Grid.",
                team=request.team,
                device=device,
                site=device.site,
                user=request.user,
            )
            if template.mapping_strategy == "site_defined":
                from .datapoint_maps import ensure_device_datapoint_map

                ensure_device_datapoint_map(device)
            else:
                transaction.on_commit(lambda: _push_gateway_config_after_commit(gateway.id, request.team.id))

        if template.mapping_strategy == "site_defined":
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse(
                "web_team:devices:device_datapoint_mapping",
                args=[request.team.slug, device.pk],
            )
            return response

        test_register = _first_readable_register(template)

        response = render(
            request,
            "devices/partials/device_quick_add_success.html",
            {
                "device": device,
                "test_register": test_register,
            },
        )
        response["HX-Trigger"] = "infrastructureChanged"
        return response

    templates = template_qs.order_by("-is_verified", "-usage_count")[:10]
    context = {
        "templates": templates,
        "suggested_templates": suggested_templates,
        "gateway": gateway,
        "gateway_id": gateway.id,
        "site_id": site.id,
        "port": port,
        "prefill": prefill_data,
        "resolve": resolve,
    }
    return render(request, "devices/partials/device_quick_add_form.html", context)


@require_permission("view_devices")
def template_library_search(request, team_slug):
    query = request.GET.get("q", "").strip()[:100]
    candidate_index = request.GET.get("candidate_index", "")
    if not re.fullmatch(r"(?:\d+|saved-\d+)", candidate_index):
        candidate_index = ""
    templates = (
        visible_templates_for_team(request.team)
        .filter(
            Q(name__icontains=query)
            | Q(manufacturer__icontains=query)
            | Q(model_number__icontains=query)
            | Q(device_type__icontains=query)
            | Q(protocol__icontains=query)
            | Q(category__icontains=query)
        )
        .order_by("-is_verified", "created_by_team_id", "name")
    )
    template_name = (
        "onboarding/partials/template_search_results.html"
        if request.GET.get("context") == "guided_setup"
        else "devices/partials/template_search_results.html"
    )
    return render(
        request,
        template_name,
        {"templates": templates[:10], "candidate_index": candidate_index, "query": query},
    )


@require_permission("manage_devices")
@require_POST
def gateway_discovery_api(request, team_slug):
    """
    API for the Edge Gateway to report discovered devices.
    Expected payload:
    {
        "serial_number": "NF-xxx",
        "discovered_devices": [
            {"port": 1, "protocol": "modbus", "slave_id": 5, "signature": "Eastron-SDM630"},
            ...
        ]
    }
    """
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    try:
        data = json.loads(request.body)
        serial = data.get("serial_number")
        discovered = data.get("discovered_devices", [])

        gateway = Gateway.objects.get(
            team=request.team,
            serial_number=serial,
            inventory_record__status="claimed",
        )
        if gateway.lifecycle_status in {"release_pending", "released"}:
            return JsonResponse({"error": "This Gateway is being securely released."}, status=409)
        from .discovery_matching import enrich_discovered_device

        discovered = [enrich_discovered_device(device) for device in discovered]

        # Store for the UI to display
        gateway.discovery_data = {"last_discovered_at": str(timezone.now()), "devices": discovered}
        gateway.save()

        from apps.events.services import log_event

        log_event(
            category="infrastructure",
            message=f"Gateway {gateway.serial_number} discovered {len(discovered)} new devices.",
            team=gateway.team,
            site=gateway.site,
            metadata={"discovered_count": len(discovered), "serial": serial},
        )

        return HttpResponse(
            json.dumps({"status": "ok", "message": "Discovery report received"}), content_type="application/json"
        )
    except Gateway.DoesNotExist:
        return HttpResponse("Gateway not found", status=404)
    except Exception as e:
        return HttpResponse(str(e), status=400)


@require_permission("manage_devices")
@require_POST
def ai_template_generate(request, team_slug):
    manufacturer = request.POST.get("manufacturer", "").strip()
    model_number = request.POST.get("model_number", "").strip()
    doc_url = request.POST.get("doc_url", "").strip() or None
    documentation_file = request.FILES.get("documentation_file")

    if not manufacturer or not model_number:
        return render(
            request,
            "devices/partials/ai_template_result.html",
            {"status": "error", "error": "Both Manufacturer and Model Number are required."},
        )

    # Check for existing template first (case-insensitive)
    existing = (
        visible_templates_for_team(request.team)
        .filter(
            manufacturer__iexact=manufacturer,
            model_number__iexact=model_number,
        )
        .first()
    )
    if existing:
        return render(
            request, "devices/partials/ai_template_result.html", {"status": "found_existing", "template": existing}
        )

    # Kick off async task
    task_id = str(uuid.uuid4())
    doc_storage_path = None
    if documentation_file:
        invalid_pdf = (
            not documentation_file.name.lower().endswith(".pdf")
            or documentation_file.size > 10 * 1024 * 1024
            or documentation_file.content_type not in {"application/pdf", "application/x-pdf"}
        )
        signature = documentation_file.read(5)
        documentation_file.seek(0)
        if invalid_pdf or signature != b"%PDF-":
            return render(
                request,
                "devices/partials/ai_template_result.html",
                {
                    "status": "error",
                    "error": "Equipment documentation must be a valid PDF no larger than 10 MB.",
                },
            )
        doc_storage_path = default_storage.save(
            f"equipment-ai-drafts/{request.team.pk}/{task_id}.pdf",
            documentation_file,
        )
    # Initialize cache status to processing
    cache.set(f"ai_template:{task_id}", {"status": "processing"}, timeout=300)
    try:
        generate_template_ai_task.delay(
            task_id,
            manufacturer,
            model_number,
            doc_url=doc_url,
            doc_storage_path=doc_storage_path,
        )
    except Exception:
        if doc_storage_path:
            default_storage.delete(doc_storage_path)
        cache.delete(f"ai_template:{task_id}")
        return render(
            request,
            "devices/partials/ai_template_result.html",
            {
                "status": "error",
                "error": "Novena could not queue the documentation review. Please retry.",
            },
        )
    return render(request, "devices/partials/ai_template_loading.html", {"task_id": task_id})


@require_permission("manage_devices")
def ai_template_status(request, team_slug, task_id):
    result = cache.get(f"ai_template:{task_id}")
    if not result or result.get("status") == "processing":
        # HTMX will poll this again
        return render(request, "devices/partials/ai_template_loading.html", {"task_id": task_id})
    elif result.get("status") == "complete":
        return render(
            request,
            "devices/partials/ai_template_result.html",
            {"status": "draft", "draft": result["draft"], "task_id": task_id},
        )
    else:
        return render(
            request,
            "devices/partials/ai_template_result.html",
            {"status": "error", "error": result.get("error", "AI template generation failed or timed out.")},
        )


@require_permission("manage_devices")
@require_POST
def ai_template_approve(request, team_slug):
    task_id = request.POST.get("task_id")
    result = cache.get(f"ai_template:{task_id}")
    if not result or result.get("status") != "complete" or "draft" not in result:
        return render(
            request,
            "devices/partials/ai_template_result.html",
            {"status": "error", "error": "Template draft expired or not found. Please try generating again."},
        )

    try:
        # Get team context from request
        team = getattr(request, "team", None)
        template = save_approved_template(result["draft"], team=team)

        # Clear cache
        cache.delete(f"ai_template:{task_id}")

        return render(request, "devices/partials/ai_template_result.html", {"status": "approved", "template": template})
    except Exception as e:
        logger.exception("Failed to save approved template")
        return render(
            request,
            "devices/partials/ai_template_result.html",
            {"status": "error", "error": f"Failed to save template: {e}"},
        )


class TemplateLibraryView(PermissionRequiredMixin, ListView):
    permission_required = "view_devices"
    model = DeviceTemplate
    template_name = "devices/template_library.html"
    context_object_name = "templates"

    def get_queryset(self):
        from apps.devices.solution_profiles import get_profile, rank_templates_for_profile

        qs = visible_templates_for_team(self.request.team)
        q = self.request.GET.get("q", "").strip()
        protocol = self.request.GET.get("protocol", "").strip()
        category = self.request.GET.get("category", "").strip()
        profile_key = self.request.GET.get("profile", "").strip()
        verified_only = self.request.GET.get("verified_only", "") == "true"

        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(manufacturer__icontains=q) | Q(model_number__icontains=q))
        if protocol:
            qs = qs.filter(protocol=protocol)
        if category:
            qs = qs.filter(category=category)
        if verified_only:
            qs = qs.filter(is_verified=True)

        if profile_key:
            return rank_templates_for_profile(qs, get_profile(profile_key))
        return qs.order_by("-is_verified", "-usage_count")

    def get_context_data(self, **kwargs):
        from apps.devices.solution_profiles import PROFILES

        context = super().get_context_data(**kwargs)
        context["active_tab"] = "templates"
        context["search_query"] = self.request.GET.get("q", "")
        context["selected_protocol"] = self.request.GET.get("protocol", "")
        context["selected_category"] = self.request.GET.get("category", "")
        context["selected_profile"] = self.request.GET.get("profile", "")
        context["verified_only"] = self.request.GET.get("verified_only", "") == "true"
        context["protocols"] = DeviceTemplate.PROTOCOL_CHOICES
        context["categories"] = DeviceTemplate.VERTICAL_CHOICES
        context["solution_profiles"] = PROFILES.values()
        return context


@require_permission("send_critical_commands")
@require_POST
def gateway_ota_update(request, team_slug, pk):
    """Trigger an OTA Firmware update on the gateway."""
    from .models import FirmwareRelease
    from .ota_signing import ensure_release_signed
    from .remote_control import CommandDenied, request_remote_command

    gateway = Gateway.objects.get(pk=pk, team=request.team)
    version = request.POST.get("version")

    release = FirmwareRelease.objects.filter(version=version, is_active=True).first()
    if not release:
        return JsonResponse({"error": "Firmware release not found."}, status=404)

    url = request.build_absolute_uri(release.file.url)
    try:
        release = ensure_release_signed(release, url)
    except ValidationError as e:
        message = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
        return JsonResponse({"error": f"Firmware release is not signed: {message}"}, status=400)

    params = {
        "manifest": release.manifest,
        "signature": release.signature,
    }

    try:
        command = request_remote_command(
            gateway=gateway,
            operation="update_firmware",
            requested_by=request.user,
            params=params,
            reason=request.POST.get("reason", f"Install signed firmware {release.version}"),
        )
    except CommandDenied as exc:
        return JsonResponse({"error": str(exc), "code": exc.code}, status=400)

    return JsonResponse({"command_id": str(command.pk), "version": release.version, "status": command.status})
