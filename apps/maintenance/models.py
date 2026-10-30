import uuid

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.devices.models import Device
from apps.teams.models import BaseTeamModel
from apps.users.models import CustomUser


class TicketTemplate(BaseTeamModel):
    """Pre-built templates with standardized checklists for common procedures."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    estimated_duration_minutes = models.PositiveIntegerField(default=60)
    checklist = models.JSONField(
        default=list, help_text=_("List of checklist items like {'task': 'Check oil', 'required': True}")
    )

    def __str__(self):
        return f"{self.name} ({self.team.name})"


class PreventiveSchedule(BaseTeamModel):
    """Configuration for recurring preventive maintenance."""

    class IntervalChoices(models.TextChoices):
        DAILY = "daily", _("Daily")
        WEEKLY = "weekly", _("Weekly")
        MONTHLY = "monthly", _("Monthly")
        QUARTERLY = "quarterly", _("Quarterly")
        YEARLY = "yearly", _("Yearly")

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="maintenance_schedules")
    template = models.ForeignKey(TicketTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    title = models.CharField(max_length=200)
    interval = models.CharField(
        max_length=20, choices=IntervalChoices.choices, default=IntervalChoices.MONTHLY, null=True, blank=True
    )
    next_due_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Usage-based PM settings
    is_usage_based = models.BooleanField(default=False)
    usage_telemetry_key = models.CharField(max_length=100, default="run_hours", blank=True)
    usage_threshold = models.FloatField(default=0.0, blank=True, null=True)
    last_trigger_usage_value = models.FloatField(default=0.0, blank=True, null=True)

    # Auto-assignment settings
    assigned_to = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_schedules"
    )
    send_email_notification = models.BooleanField(default=True, verbose_name="Notify assignee via Email")
    send_whatsapp_notification = models.BooleanField(default=False, verbose_name="Notify assignee via WhatsApp")

    def __str__(self):
        return f"PM: {self.title} on {self.device.name}"

    @property
    def needs_setup(self):
        if self.is_usage_based:
            return not self.usage_telemetry_key or not self.usage_threshold or self.usage_threshold <= 0
        return not self.next_due_at or not self.interval


class MaintenanceTicket(BaseTeamModel):
    """The core ticket model representing a reactive or scheduled maintenance job."""

    class PriorityChoices(models.TextChoices):
        LOW = "low", _("Low")
        MEDIUM = "medium", _("Medium")
        HIGH = "high", _("High")
        CRITICAL = "critical", _("Critical")

    class StatusChoices(models.TextChoices):
        OPEN = "open", _("Open")
        IN_PROGRESS = "in_progress", _("In Progress")
        WAITING = "waiting", _("Waiting on Parts/Vendor")
        RESOLVED = "resolved", _("Resolved (Pending Verification)")
        CLOSED = "closed", _("Closed")

    class TypeChoices(models.TextChoices):
        REACTIVE = "reactive", _("Reactive (Alert/Breakdown)")
        PREVENTIVE = "preventive", _("Preventive (Scheduled)")

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="tickets")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    ticket_type = models.CharField(max_length=50, choices=TypeChoices.choices, default=TypeChoices.REACTIVE)
    priority = models.CharField(max_length=20, choices=PriorityChoices.choices, default=PriorityChoices.MEDIUM)
    status = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.OPEN)

    assigned_to = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tickets"
    )
    reported_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="reported_tickets"
    )

    # Traceability
    alert_reference = models.CharField(
        max_length=100, blank=True, help_text=_("UUID of the alert that triggered this ticket, if any")
    )
    schedule_reference = models.ForeignKey(PreventiveSchedule, on_delete=models.SET_NULL, null=True, blank=True)
    send_email_notification = models.BooleanField(default=True, verbose_name="Notify assignee via Email")
    send_whatsapp_notification = models.BooleanField(default=False, verbose_name="Notify assignee via WhatsApp")

    # Process tracking
    due_date = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    downtime_minutes = models.PositiveIntegerField(default=0)
    labor_minutes = models.PositiveIntegerField(
        default=0,
        blank=True,
        help_text=_("Actual hands-on labor recorded for this work item."),
    )

    # Template instances mapping
    checklist_state = models.JSONField(
        default=list, help_text=_("Instance copy of the template checklist with completion states")
    )

    closed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"TKT-{self.id}: {self.title}"

    @property
    def alert(self):
        if not self.alert_reference:
            return None
        from apps.alerts.models import Alert

        try:
            return Alert.objects.filter(id=self.alert_reference, team=self.team).first()
        except (ValueError, TypeError):
            return None

    def save(self, *args, **kwargs):
        if self.status in [self.StatusChoices.CLOSED, self.StatusChoices.RESOLVED] and not self.closed_at:
            self.closed_at = timezone.now()
        super().save(*args, **kwargs)


class TicketComment(BaseTeamModel):
    """Threaded notes, updates, and attachments appended to a ticket."""

    ticket = models.ForeignKey(MaintenanceTicket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    content = models.TextField()
    is_system_generated = models.BooleanField(default=False)
    attachment = models.FileField(
        upload_to="maintenance/attachments/",
        blank=True,
        null=True,
        help_text="Upload signed logsheets, checklist photos, or certificates (PDF, JPG, PNG up to 10MB)",
    )
    guest_name = models.CharField(max_length=100, blank=True, null=True)

    def __str__(self):
        if self.author:
            return f"Comment by {self.author} on {self.ticket}"
        return f"Comment by {self.guest_name or 'External Contractor'} on {self.ticket}"


class SharedTicketLink(BaseTeamModel):
    """A secure public shareable link to a maintenance ticket for guest contractors."""

    ticket = models.ForeignKey(MaintenanceTicket, on_delete=models.CASCADE, related_name="shared_links")
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text=_("Optional expiration date"))
    auto_revoke_on_resolve = models.BooleanField(
        default=True, help_text=_("Deactivate this link automatically once the ticket is resolved")
    )
    view_count = models.PositiveIntegerField(default=0)
    last_viewed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Share Link for TKT-{self.ticket.id} ({self.team.name})"

    @property
    def is_expired(self):
        return bool(self.expires_at and timezone.now() > self.expires_at)
