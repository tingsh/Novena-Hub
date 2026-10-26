from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.dashboard.forms import SharedDashboardForm
from apps.dashboard.models import SharedDashboard
from apps.teams.mixins import PermissionRequiredMixin


class SharedDashboardListView(PermissionRequiredMixin, ListView):
    permission_required = "manage_shared_links"
    model = SharedDashboard
    template_name = "dashboard/team/shared_list.html"
    context_object_name = "links"

    def get_queryset(self):
        return SharedDashboard.objects.filter(team=self.request.team)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "shared-links"
        return context


class SharedDashboardCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_shared_links"
    model = SharedDashboard
    form_class = SharedDashboardForm
    template_name = "dashboard/team/shared_form.html"

    def get_initial(self):
        initial = super().get_initial()
        initial["is_active"] = False
        return initial

    def form_valid(self, form):
        form.instance.team = self.request.team
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("web_team:dashboard_team:list", args=[self.request.team.slug])


class SharedDashboardUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_shared_links"
    model = SharedDashboard
    form_class = SharedDashboardForm
    template_name = "dashboard/team/shared_form.html"

    def get_queryset(self):
        return SharedDashboard.objects.filter(team=self.request.team)

    def get_success_url(self):
        return reverse_lazy("web_team:dashboard_team:list", args=[self.request.team.slug])


class SharedDashboardDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = "manage_shared_links"
    model = SharedDashboard
    template_name = "dashboard/team/shared_confirm_delete.html"

    def get_queryset(self):
        return SharedDashboard.objects.filter(team=self.request.team)

    def get_success_url(self):
        return reverse_lazy("web_team:dashboard_team:list", args=[self.request.team.slug])
