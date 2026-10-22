from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from apps.teams.mixins import PermissionRequiredMixin

from .forms import ActionFormSet, AutomationForm, ConditionFormSet
from .models import Automation, AutomationLog


def _has_live_rows(formset):
    return any(row.cleaned_data and not row.cleaned_data.get("DELETE") for row in formset.forms)


def _save_automation_rows(formset, team):
    rows = formset.save(commit=False)
    for deleted in formset.deleted_objects:
        deleted.delete()
    for row in rows:
        row.team = team
        row.save()
    formset.save_m2m()


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

        context["active_tab"] = "automations"
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        conditions = context["conditions"]
        actions = context["actions"]

        conditions_valid = conditions.is_valid()
        actions_valid = actions.is_valid()
        if not conditions_valid or not actions_valid:
            return self.form_invalid(form)
        if form.cleaned_data.get("is_active"):
            if not _has_live_rows(conditions):
                form.add_error(None, "Add at least one equipment reading before enabling this automation.")
            if not _has_live_rows(actions):
                form.add_error(None, "Add at least one action before enabling this automation.")
            if form.errors:
                return self.form_invalid(form)

        with transaction.atomic():
            form.instance.team = self.request.team
            self.object = form.save()
            conditions.instance = self.object
            _save_automation_rows(conditions, self.request.team)
            actions.instance = self.object
            _save_automation_rows(actions, self.request.team)

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

        context["active_tab"] = "automations"
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        conditions = context["conditions"]
        actions = context["actions"]

        conditions_valid = conditions.is_valid()
        actions_valid = actions.is_valid()
        if not conditions_valid or not actions_valid:
            return self.form_invalid(form)
        if form.cleaned_data.get("is_active"):
            if not _has_live_rows(conditions):
                form.add_error(None, "Add at least one equipment reading before enabling this automation.")
            if not _has_live_rows(actions):
                form.add_error(None, "Add at least one action before enabling this automation.")
            if form.errors:
                return self.form_invalid(form)

        with transaction.atomic():
            self.object = form.save()
            _save_automation_rows(conditions, self.request.team)
            _save_automation_rows(actions, self.request.team)

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
