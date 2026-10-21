import json

from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from apps.devices.datapoint_maps import effective_register_map
from apps.devices.models import Device
from apps.teams.mixins import PermissionRequiredMixin

from .forms import ActionFormSet, AutomationForm, ConditionFormSet
from .models import Automation, AutomationLog


class AutomationListView(PermissionRequiredMixin, ListView):
    permission_required = "view_automations"
    model = Automation
    template_name = "automations/automation_list.html"
    context_object_name = "automations"

    def get_queryset(self):
        return Automation.objects.filter(team=self.request.team).prefetch_related("conditions", "actions")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "automations"
        context["page_title"] = "Logic & Automations"
        return context


class AutomationLogListView(PermissionRequiredMixin, ListView):
    permission_required = "view_automations"
    model = AutomationLog
    template_name = "automations/automation_logs.html"
    context_object_name = "logs"
    paginate_by = 50

    def get_queryset(self):
        return AutomationLog.objects.filter(team=self.request.team).order_by("-triggered_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "automations"
        context["page_title"] = "Automation Audit Logs"
        return context


class AutomationDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "view_automations"
    model = Automation
    template_name = "automations/automation_detail.html"
    context_object_name = "automation"

    def get_queryset(self):
        return Automation.objects.filter(team=self.request.team)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "automations"
        context["recent_logs"] = self.object.logs.order_by("-triggered_at")[:5]
        return context


class AutomationCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_automations"
    model = Automation
    form_class = AutomationForm
    template_name = "automations/automation_form.html"

    def get_initial(self):
        initial = super().get_initial()
        initial["is_active"] = False
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["conditions"] = ConditionFormSet(self.request.POST, form_kwargs={"team": self.request.team})
            context["actions"] = ActionFormSet(self.request.POST, form_kwargs={"team": self.request.team})
        else:
            context["conditions"] = ConditionFormSet(form_kwargs={"team": self.request.team})
            context["actions"] = ActionFormSet(form_kwargs={"team": self.request.team})

        # Prepare devices data for the step-by-step logic builder
        devices_data = {}
        for device in Device.objects.filter(team=self.request.team).select_related("template"):
            register_map = effective_register_map(device)
            keys = list(register_map.keys())
            site_defined = getattr(device.template, "mapping_strategy", "fixed") == "site_defined"
            if not keys and not site_defined:
                keys = ["temp", "humidity", "pressure", "voltage", "current", "status"]

            writable_keys = [k for k, v in register_map.items() if isinstance(v, dict) and v.get("writable")]
            if not writable_keys and not site_defined:
                writable_keys = ["turn_on", "turn_off", "set_speed", "reset"]

            devices_data[str(device.id)] = {
                "name": device.name,
                "telemetry_keys": keys,
                "command_keys": writable_keys,
            }
        context["devices_json"] = json.dumps(devices_data)
        context["active_tab"] = "automations"
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        conditions = context["conditions"]
        actions = context["actions"]

        if not conditions.is_valid() or not actions.is_valid():
            return self.form_invalid(form)

        with transaction.atomic():
            form.instance.team = self.request.team
            self.object = form.save()
            conditions.instance = self.object
            conditions.save()
            actions.instance = self.object
            actions.save()

        messages.success(self.request, "Automation created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("web_team:automations:detail", args=[self.request.team.slug, self.object.id])


class AutomationUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_automations"
    model = Automation
    form_class = AutomationForm
    template_name = "automations/automation_form.html"

    def get_queryset(self):
        return Automation.objects.filter(team=self.request.team)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["conditions"] = ConditionFormSet(
                self.request.POST, instance=self.object, form_kwargs={"team": self.request.team}
            )
            context["actions"] = ActionFormSet(
                self.request.POST, instance=self.object, form_kwargs={"team": self.request.team}
            )
        else:
            context["conditions"] = ConditionFormSet(instance=self.object, form_kwargs={"team": self.request.team})
            context["actions"] = ActionFormSet(instance=self.object, form_kwargs={"team": self.request.team})

        # Prepare devices data for the step-by-step logic builder
        devices_data = {}
        for device in Device.objects.filter(team=self.request.team).select_related("template"):
            register_map = effective_register_map(device)
            keys = list(register_map.keys())
            site_defined = getattr(device.template, "mapping_strategy", "fixed") == "site_defined"
            if not keys and not site_defined:
                keys = ["temp", "humidity", "pressure", "voltage", "current", "status"]

            writable_keys = [k for k, v in register_map.items() if isinstance(v, dict) and v.get("writable")]
            if not writable_keys and not site_defined:
                writable_keys = ["turn_on", "turn_off", "set_speed", "reset"]

            devices_data[str(device.id)] = {
                "name": device.name,
                "telemetry_keys": keys,
                "command_keys": writable_keys,
            }
        context["devices_json"] = json.dumps(devices_data)
        context["active_tab"] = "automations"
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        conditions = context["conditions"]
        actions = context["actions"]

        if not conditions.is_valid() or not actions.is_valid():
            return self.form_invalid(form)

        with transaction.atomic():
            self.object = form.save()
            conditions.save()
            actions.save()

        messages.success(self.request, "Automation updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("web_team:automations:detail", args=[self.request.team.slug, self.object.id])


class AutomationDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = "manage_automations"
    model = Automation

    def get_queryset(self):
        return Automation.objects.filter(team=self.request.team)

    def get_success_url(self):
        messages.success(self.request, "Automation deleted.")
        return reverse("web_team:automations:list", args=[self.request.team.slug])
