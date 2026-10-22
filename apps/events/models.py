from django.conf import settings
from django.db import models

from apps.teams.models import BaseTeamModel, Team


class ActivityLog(BaseTeamModel):
    CATEGORY_CHOICES = (
        ("infrastructure", "Infrastructure"),
        ("alert", "Alert"),
        ("audit", "Action Log"),
    )

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    message = models.TextField()

    # Optional links to objects
    device = models.ForeignKey("devices.Device", on_delete=models.CASCADE, null=True, blank=True)
    site = models.ForeignKey("devices.Site", on_delete=models.CASCADE, null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name_plural = "Activity logs"

    def __str__(self):
        return f"[{self.category}] {self.message[:50]}..."


class EmailDelivery(models.Model):
    class NotificationType(models.TextChoices):
        ALERT_TRIGGERED = "alert_triggered", "Alert triggered"
        ALERT_RESOLVED = "alert_resolved", "Alert resolved"
        AUTOMATION = "automation", "Automation"
        TEAM_INVITATION = "team_invitation", "Team invitation"
        MAINTENANCE_ASSIGNMENT = "maintenance_assignment", "Maintenance assignment"
        MAINTENANCE_RESOLUTION = "maintenance_resolution", "Maintenance resolution"
        SUPPORT_REQUEST = "support_request", "Support request"
        SALES_INQUIRY = "sales_inquiry", "Sales inquiry"
        IMPACT_REPORT = "impact_report", "Business impact report"
        TEST_EMAIL = "test_email", "Test email"
        UNKNOWN = "unknown", "Unknown"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Complained"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"
        DEFERRED = "deferred", "Deferred"
        NO_RECIPIENT = "no_recipient", "No recipient"
        UNKNOWN = "unknown", "Unknown"

    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True, related_name="email_deliveries")
    notification_type = models.CharField(
        max_length=40, choices=NotificationType.choices, default=NotificationType.UNKNOWN, db_index=True
    )
    recipient = models.EmailField(db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    alert = models.ForeignKey("alerts.Alert", on_delete=models.SET_NULL, null=True, blank=True)
    invitation = models.ForeignKey("teams.Invitation", on_delete=models.SET_NULL, null=True, blank=True)
    maintenance_ticket = models.ForeignKey(
        "maintenance.MaintenanceTicket", on_delete=models.SET_NULL, null=True, blank=True
    )
    provider_message_id = models.CharField(max_length=255, blank=True, db_index=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    last_event_id = models.CharField(max_length=255, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "notification_type", "status"]),
            models.Index(fields=["provider_message_id", "recipient"]),
        ]
        verbose_name_plural = "Email deliveries"

    def __str__(self):
        return f"{self.notification_type} to {self.recipient} ({self.status})"
