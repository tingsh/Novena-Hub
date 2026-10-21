from urllib.parse import urlencode

from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.devices.datapoint_maps import effective_register_map
from apps.devices.models import Device
from apps.devices.solution_profiles import recommended_alerts_for_device
from apps.teams.decorators import require_permission
from apps.teams.mixins import PermissionRequiredMixin

from .forms import AlertRuleForm
from .models import Alert, AlertRule


class AlertListView(PermissionRequiredMixin, ListView):
    permission_required = "view_dashboard"
    model = Alert
    template_name = "alerts/alert_list.html"
    context_object_name = "alerts"

    def get_queryset(self):
        return Alert.objects.filter(team=self.request.team).order_by("-triggered_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "alerts"
        context["rules"] = AlertRule.objects.filter(team=self.request.team)
        context["active_rule_count"] = context["rules"].filter(is_active=True).count()
        return context


# Alert Rule CRUD


def _suggestions_for_device(device):
    """Return editable alert ideas; reading a template never creates a rule."""
    suggestions = []
    seen = set()
    for preset in recommended_alerts_for_device(device):
        item = {
            "name": f"{device.name} {preset.name}",
            "telemetry_key": preset.key,
            "condition": preset.condition,
            "threshold": preset.threshold,
            "severity": preset.severity,
            "duration_seconds": preset.duration_seconds,
        }
        suggestions.append(item)
        seen.add((item["telemetry_key"], item["condition"], float(item["threshold"])))
    for preset in (device.template.alert_presets or []) if device.template else []:
        if not isinstance(preset, dict):
            continue
        if not all(key in preset for key in ("key", "condition", "threshold")):
            continue
        item = {
            "name": f"{device.name} {str(preset['key']).replace('_', ' ').title()}",
            "telemetry_key": preset["key"],
            "condition": preset["condition"],
            "threshold": preset["threshold"],
            "severity": preset.get("severity", "warning"),
            "duration_seconds": preset.get("duration_seconds", 0),
        }
        try:
            signature = (item["telemetry_key"], item["condition"], float(item["threshold"]))
        except (TypeError, ValueError):
            continue
        if signature not in seen:
            suggestions.append(item)
            seen.add(signature)
    return suggestions


def _suggestion_display(device, preset):
    key = preset["telemetry_key"]
    config = effective_register_map(device).get(key, {})
    if not isinstance(config, dict):
        config = {}
    label = config.get("label") or key.replace("_", " ").title()
    condition = preset["condition"]
    threshold = preset["threshold"]
    if key == "door_open" and condition == "eq":
        return "Door opens" if float(threshold) else "Door closes"
    if key == "compressor_status" and condition == "eq":
        return f"{label} turns {'on' if float(threshold) else 'off'}"
    comparison = {
        "gt": "above", "gte": "at or above", "lt": "below", "lte": "at or below",
        "eq": "equal to", "neq": "not equal to",
    }.get(condition, condition)
    unit = {"degC": "°C", "degF": "°F"}.get(config.get("unit"), config.get("unit", ""))
    number = f"{float(threshold):g}"
    return f"{label} {comparison} {number}{' ' + unit if unit else ''}"


class AlertRuleListView(PermissionRequiredMixin, ListView):
    permission_required = "view_dashboard"
    model = AlertRule
    template_name = "alerts/rule_list.html"
    context_object_name = "rules"

    def get_queryset(self):
        return AlertRule.objects.filter(team=self.request.team)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "alert_rules"
        suggestions = []
        create_url = reverse("web_team:alerts:rule_create", args=[self.request.team.slug])
        for device in Device.objects.filter(team=self.request.team).select_related("site", "template"):
            for index, preset in enumerate(_suggestions_for_device(device)):
                suggestions.append({
                    "device": device,
                    "preset": preset,
                    "display": _suggestion_display(device, preset),
                    "url": f"{create_url}?{urlencode({'device': device.pk, 'preset': index})}",
                })
        context["suggestions"] = suggestions
        return context


class AlertRuleCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_alerts"
    model = AlertRule
    form_class = AlertRuleForm
    template_name = "alerts/rule_form.html"

    def get_initial(self):
        initial = super().get_initial()
        device_id = self.request.GET.get("device")
        preset_index = self.request.GET.get("preset")
        if not device_id or preset_index is None:
            return initial
        try:
            device = (
                Device.objects.filter(team=self.request.team, pk=device_id)
                .select_related("site", "template")
                .first()
            )
        except (ValueError, TypeError):
            return initial
        if not device:
            return initial
        try:
            index = int(preset_index)
            if index < 0:
                return initial
            preset = _suggestions_for_device(device)[index]
        except (ValueError, IndexError):
            return initial
        initial.update(preset)
        initial.update({"device": device, "site": device.site, "is_active": False, "notify_email": False})
        return initial

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["team"] = self.request.team
        return kwargs

    def form_valid(self, form):
        form.instance.team = self.request.team
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("web_team:alerts:alert_list", args=[self.request.team.slug])


class AlertRuleUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_alerts"
    model = AlertRule
    form_class = AlertRuleForm
    template_name = "alerts/rule_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["team"] = self.request.team
        return kwargs

    def get_success_url(self):
        return reverse_lazy("web_team:alerts:alert_list", args=[self.request.team.slug])


class AlertRuleDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = "manage_alerts"
    model = AlertRule
    template_name = "alerts/rule_confirm_delete.html"

    def get_success_url(self):
        return reverse_lazy("web_team:alerts:alert_list", args=[self.request.team.slug])


@require_permission("acknowledge_alerts")
@require_POST
def acknowledge_alert(request, team_slug, alert_id):
    """
    HTMX view to acknowledge an alert.
    Returns the updated alert partial or a success indicator.
    """
    alert = get_object_or_404(Alert, id=alert_id, team=request.team)
    if alert.status == "active":
        alert.status = "acknowledged"
        alert.acknowledged_at = timezone.now()
        alert.acknowledged_by = request.user
        alert.save(update_fields=["status", "acknowledged_at", "acknowledged_by"])

    # Return a partial or just the updated row
    return render(request, "alerts/partials/alert_row.html", {"alert": alert})


@require_permission("manage_alerts")
def search_team_members(request, team_slug):
    q = request.GET.get("q", "").strip()
    if not q:
        return render(request, "alerts/partials/user_search_results.html", {"users": []})

    users = request.team.members.filter(
        Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q) | Q(username__icontains=q)
    )[:10]

    return render(request, "alerts/partials/user_search_results.html", {"users": users})


@require_permission("manage_maintenance")
@require_POST
def escalate_alert_to_ticket(request, team_slug, alert_id):
    """
    HTMX POST view to manually escalate an alert into a reactive maintenance ticket.
    Uses the auto_create_ticket service with force=True.
    """
    from apps.alerts.models import Alert
    from apps.maintenance.services import auto_create_ticket

    alert = get_object_or_404(Alert, id=alert_id, team=request.team)

    ticket = alert.ticket
    if not ticket:
        auto_create_ticket(alert, force=True)

    return render(request, "alerts/partials/alert_row.html", {"alert": alert})
