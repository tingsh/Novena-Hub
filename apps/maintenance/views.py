from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.teams.decorators import login_and_team_required
from apps.teams.mixins import PermissionRequiredMixin
from apps.teams.roles import visible_sites

from .forms import (
    MaintenanceTicketForm,
    PreventiveScheduleForm,
    SharedTicketLinkForm,
    TicketCommentForm,
    TicketTemplateForm,
)
from .models import MaintenanceTicket, PreventiveSchedule, SharedTicketLink, TicketTemplate

# --- Tickets ---


class TicketListView(PermissionRequiredMixin, ListView):
    permission_required = "view_maintenance"
    model = MaintenanceTicket
    template_name = "maintenance/ticket_list.html"
    context_object_name = "tickets"

    def get_queryset(self):
        qs = visible_sites(
            MaintenanceTicket.objects.filter(team=self.request.team),
            self.request.user,
            self.request.team,
            lookup="device__site_id",
        )

        status_filter = self.request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        context["status_filter"] = self.request.GET.get("status", "")

        # Fetch and group all tickets for the Kanban view
        all_tickets = visible_sites(
            MaintenanceTicket.objects.filter(team=self.request.team),
            self.request.user,
            self.request.team,
            lookup="device__site_id",
        ).order_by("-created_at")
        context["open_tickets"] = all_tickets.filter(status=MaintenanceTicket.StatusChoices.OPEN)
        context["in_progress_tickets"] = all_tickets.filter(status=MaintenanceTicket.StatusChoices.IN_PROGRESS)
        context["waiting_tickets"] = all_tickets.filter(status=MaintenanceTicket.StatusChoices.WAITING)
        context["completed_tickets"] = all_tickets.filter(
            status__in=[MaintenanceTicket.StatusChoices.RESOLVED, MaintenanceTicket.StatusChoices.CLOSED]
        )

        # Calculate banner stats
        context["stats"] = {
            "total": all_tickets.count(),
            "critical_count": all_tickets.filter(priority=MaintenanceTicket.PriorityChoices.CRITICAL)
            .exclude(status__in=["resolved", "closed"])
            .count(),
            "open_count": all_tickets.filter(status=MaintenanceTicket.StatusChoices.OPEN).count(),
            "in_progress_count": all_tickets.filter(status=MaintenanceTicket.StatusChoices.IN_PROGRESS).count(),
            "waiting_count": all_tickets.filter(status=MaintenanceTicket.StatusChoices.WAITING).count(),
            "completed_count": all_tickets.filter(status__in=["resolved", "closed"]).count(),
        }
        return context


class TicketDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "view_maintenance"
    model = MaintenanceTicket
    template_name = "maintenance/ticket_detail.html"
    context_object_name = "ticket"

    def get_queryset(self):
        return visible_sites(
            MaintenanceTicket.objects.filter(team=self.request.team),
            self.request.user,
            self.request.team,
            lookup="device__site_id",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        context["comment_form"] = TicketCommentForm()
        context["shared_links"] = SharedTicketLink.objects.filter(ticket=self.object, is_active=True).order_by(
            "-created_at"
        )
        context["share_form"] = SharedTicketLinkForm()
        return context


class TicketCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_maintenance"
    model = MaintenanceTicket
    form_class = MaintenanceTicketForm
    template_name = "maintenance/ticket_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["team"] = self.request.team
        return kwargs

    def form_valid(self, form):
        form.instance.team = self.request.team
        form.instance.reported_by = self.request.user
        messages.success(self.request, "Ticket created successfully.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("web_team:maintenance:ticket_detail", args=[self.request.team.slug, self.object.id])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        return context


class TicketUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_maintenance"
    model = MaintenanceTicket
    form_class = MaintenanceTicketForm
    template_name = "maintenance/ticket_form.html"

    def get_queryset(self):
        return MaintenanceTicket.objects.filter(team=self.request.team)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["team"] = self.request.team
        return kwargs

    def get_success_url(self):
        messages.success(self.request, "Ticket updated successfully.")
        return reverse("web_team:maintenance:ticket_detail", args=[self.request.team.slug, self.object.id])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        return context


@require_POST
@login_and_team_required
def add_ticket_comment(request, team_slug, pk):
    team = request.team
    ticket = get_object_or_404(MaintenanceTicket, team=team, pk=pk)

    # Needs view permission minimum
    from apps.teams.roles import has_permission

    if not has_permission(request.user, team, "view_maintenance"):
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden()

    form = TicketCommentForm(request.POST, request.FILES)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.team = team
        comment.ticket = ticket
        comment.author = request.user
        comment.save()
        messages.success(request, "Comment added.")
    return redirect("web_team:maintenance:ticket_detail", team_slug=team.slug, pk=ticket.id)


# --- Preventive Schedules ---


class ScheduleListView(PermissionRequiredMixin, ListView):
    permission_required = "view_maintenance"
    model = PreventiveSchedule
    template_name = "maintenance/schedule_list.html"
    context_object_name = "schedules"

    def get_queryset(self):
        return visible_sites(
            PreventiveSchedule.objects.filter(team=self.request.team),
            self.request.user,
            self.request.team,
            lookup="device__site_id",
        ).order_by("next_due_at")

    def get_context_data(self, **kwargs):
        from datetime import timedelta

        from django.utils import timezone

        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        context["now"] = timezone.now()
        context["due_soon_threshold"] = timezone.now() + timedelta(days=3)
        return context


class ScheduleCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_maintenance"
    model = PreventiveSchedule
    form_class = PreventiveScheduleForm
    template_name = "maintenance/schedule_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["team"] = self.request.team
        return kwargs

    def form_valid(self, form):
        form.instance.team = self.request.team
        messages.success(self.request, "Preventive Schedule created.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("web_team:maintenance:schedule_list", args=[self.request.team.slug])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        return context


class ScheduleUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_maintenance"
    model = PreventiveSchedule
    form_class = PreventiveScheduleForm
    template_name = "maintenance/schedule_form.html"

    def get_queryset(self):
        return PreventiveSchedule.objects.filter(team=self.request.team)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["team"] = self.request.team
        return kwargs

    def get_success_url(self):
        messages.success(self.request, "Preventive Schedule updated successfully.")
        return reverse("web_team:maintenance:schedule_list", args=[self.request.team.slug])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        return context


# --- Templates ---


class TemplateListView(PermissionRequiredMixin, ListView):
    permission_required = "view_maintenance"
    model = TicketTemplate
    template_name = "maintenance/template_list.html"
    context_object_name = "templates"

    def get_queryset(self):
        return TicketTemplate.objects.filter(team=self.request.team)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        return context


class TemplateCreateView(PermissionRequiredMixin, CreateView):
    permission_required = "manage_maintenance"
    model = TicketTemplate
    form_class = TicketTemplateForm
    template_name = "maintenance/template_form.html"

    def form_valid(self, form):
        form.instance.team = self.request.team
        messages.success(self.request, "Template created.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("web_team:maintenance:template_list", args=[self.request.team.slug])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "maintenance"
        return context


class TemplateUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = "manage_maintenance"
    model = TicketTemplate
    form_class = TicketTemplateForm
    template_name = "maintenance/template_form.html"

    def get_success_url(self):
        return reverse("web_team:maintenance:template_list", args=[self.request.team.slug])


@require_POST
@login_and_team_required
def update_ticket_status(request, team_slug, pk):
    """Update maintenance ticket status, log a system comment, and return the Kanban partial."""
    from apps.teams.roles import has_permission

    if not has_permission(request.user, request.team, "manage_maintenance"):
        return HttpResponse("Forbidden", status=403)

    ticket = get_object_or_404(MaintenanceTicket, team=request.team, pk=pk)
    new_status = request.POST.get("status") or request.GET.get("status")

    if new_status in MaintenanceTicket.StatusChoices.values:
        old_status = ticket.get_status_display()
        ticket.status = new_status
        ticket.save()

        # Generate audit/activity feed comment
        from .models import TicketComment

        TicketComment.objects.create(
            team=request.team,
            ticket=ticket,
            author=request.user,
            content=f"Changed status from '{old_status}' to '{ticket.get_status_display()}'.",
            is_system_generated=True,
        )

        if request.headers.get("HX-Request"):
            # Return updated Kanban board HTML fragment
            all_tickets = MaintenanceTicket.objects.filter(team=request.team).order_by("-created_at")
            context = {
                "request": request,
                "tickets": all_tickets,
                "open_tickets": all_tickets.filter(status=MaintenanceTicket.StatusChoices.OPEN),
                "in_progress_tickets": all_tickets.filter(status=MaintenanceTicket.StatusChoices.IN_PROGRESS),
                "waiting_tickets": all_tickets.filter(status=MaintenanceTicket.StatusChoices.WAITING),
                "completed_tickets": all_tickets.filter(
                    status__in=[MaintenanceTicket.StatusChoices.RESOLVED, MaintenanceTicket.StatusChoices.CLOSED]
                ),
                "stats": {
                    "total": all_tickets.count(),
                    "critical_count": all_tickets.filter(priority=MaintenanceTicket.PriorityChoices.CRITICAL)
                    .exclude(status__in=["resolved", "closed"])
                    .count(),
                    "open_count": all_tickets.filter(status=MaintenanceTicket.StatusChoices.OPEN).count(),
                    "in_progress_count": all_tickets.filter(status=MaintenanceTicket.StatusChoices.IN_PROGRESS).count(),
                    "waiting_count": all_tickets.filter(status=MaintenanceTicket.StatusChoices.WAITING).count(),
                    "completed_count": all_tickets.filter(status__in=["resolved", "closed"]).count(),
                },
            }
            return render(request, "maintenance/partials/kanban_board.html", context)

        return redirect("web_team:maintenance:ticket_list", team_slug=request.team.slug)

    return HttpResponse("Invalid status", status=400)


@require_POST
@login_and_team_required
def toggle_checklist_item(request, team_slug, pk, item_index):
    """Toggle a specific checklist item's completion state and log audit trail."""
    from apps.teams.roles import has_permission

    if not has_permission(request.user, request.team, "manage_maintenance"):
        return HttpResponse("Forbidden", status=403)

    ticket = get_object_or_404(MaintenanceTicket, team=request.team, pk=pk)

    try:
        checklist = list(ticket.checklist_state)
        if 0 <= item_index < len(checklist):
            item = checklist[item_index]
            item["done"] = not item.get("done", False)
            ticket.checklist_state = checklist
            ticket.save()

            # Create system comment for audit log
            from .models import TicketComment

            state_label = "completed" if item["done"] else "incomplete"
            TicketComment.objects.create(
                team=request.team,
                ticket=ticket,
                author=request.user,
                content=f"Marked task '{item['task']}' as {state_label}.",
                is_system_generated=True,
            )

            if request.headers.get("HX-Request"):
                return render(request, "maintenance/partials/checklist.html", {"ticket": ticket})
    except Exception as e:
        return HttpResponse(str(e), status=400)

    return redirect("web_team:maintenance:ticket_detail", team_slug=request.team.slug, pk=ticket.id)


@require_POST
@login_and_team_required
def generate_shared_link(request, team_slug, pk):
    """Generate a public shared compliance link for a ticket."""
    from apps.teams.roles import has_permission

    if not has_permission(request.user, request.team, "manage_maintenance"):
        return HttpResponse("Forbidden", status=403)

    ticket = get_object_or_404(MaintenanceTicket, team=request.team, pk=pk)
    form = SharedTicketLinkForm(request.POST)
    if form.is_valid():
        link = form.save(commit=False)
        link.ticket = ticket
        link.team = request.team
        link.created_by = request.user
        link.save()
        messages.success(request, "Compliance shareable link generated.")
    else:
        for error_list in form.errors.values():
            for error in error_list:
                messages.error(request, error)

    return redirect("web_team:maintenance:ticket_detail", team_slug=request.team.slug, pk=ticket.id)


@require_POST
@login_and_team_required
def revoke_shared_link(request, team_slug, pk, link_pk):
    """Deactivate/Revoke a shared compliance link."""
    from apps.teams.roles import has_permission

    if not has_permission(request.user, request.team, "manage_maintenance"):
        return HttpResponse("Forbidden", status=403)

    link = get_object_or_404(SharedTicketLink, team=request.team, pk=link_pk, ticket_id=pk)
    link.is_active = False
    link.save()
    messages.success(request, "Shareable link revoked successfully.")
    return redirect("web_team:maintenance:ticket_detail", team_slug=request.team.slug, pk=pk)


@require_POST
@login_and_team_required
def trigger_preventive_schedule(request, team_slug, pk):
    """
    Manually triggers a PreventiveSchedule to generate a MaintenanceTicket immediately,
    and advances its next due date by one interval.
    """
    from apps.teams.roles import has_permission

    if not has_permission(request.user, request.team, "manage_maintenance"):
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden()

    schedule = get_object_or_404(PreventiveSchedule, team=request.team, pk=pk)

    current_val = None
    if schedule.is_usage_based:
        from apps.telemetry.services import get_latest_telemetry_value

        current_val = get_latest_telemetry_value(schedule.device, schedule.usage_telemetry_key)
        if current_val is not None:
            try:
                schedule.last_trigger_usage_value = float(current_val)
                schedule.save(update_fields=["last_trigger_usage_value"])
            except (ValueError, TypeError):
                pass

    from .services import advance_schedule_due_date, create_pm_ticket

    ticket = create_pm_ticket(schedule, current_usage=current_val)
    advance_schedule_due_date(schedule)

    messages.success(request, f"Generated ticket TKT-{ticket.id} successfully.")
    return redirect("web_team:maintenance:ticket_detail", team_slug=request.team.slug, pk=ticket.id)
