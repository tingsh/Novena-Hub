from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class AlertRule(BaseTeamModel):
    """User-defined alert rule."""

    CONDITION_CHOICES = (
        ("gt", _("Greater Than (> )")),
        ("lt", _("Less Than (< )")),
        ("gte", _("Greater Than or Equal (>= )")),
        ("lte", _("Less Than or Equal (<= )")),
        ("eq", _("Equal (== )")),
        ("neq", _("Not Equal (!= )")),
    )
    SEVERITY_CHOICES = (
        ("info", _("Info")),
        ("warning", _("Warning")),
        ("critical", _("Critical")),
    )

    name = models.CharField(max_length=200)
    device = models.ForeignKey(
        "devices.Device", on_delete=models.CASCADE, related_name="alert_rules", null=True, blank=True
    )
    site = models.ForeignKey(
        "devices.Site", on_delete=models.CASCADE, related_name="alert_rules", null=True, blank=True
    )

    telemetry_key = models.CharField(max_length=100, help_text=_("The key to monitor (e.g., 'temperature')"))
    condition = models.CharField(max_length=10, choices=CONDITION_CHOICES)
    threshold = models.FloatField()

    duration_seconds = models.IntegerField(
        default=0, help_text=_("How long the condition must persist before triggering an alert")
    )
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default="warning")

    is_active = models.BooleanField(default=True)
    notify_email = models.BooleanField(default=True)
    notify_whatsapp = models.BooleanField(
        default=False, help_text=_("Send out real-time alerts via WhatsApp. Requires a configured WhatsApp provider.")
    )
    notify_webhook = models.URLField(blank=True)
    create_maintenance_ticket = models.BooleanField(
        default=False, help_text=_("Automatically create a maintenance ticket when this alert triggers")
    )
    maintenance_template = models.ForeignKey(
        "maintenance.TicketTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text=_("Optional checklist template to attach to the generated ticket"),
    )
    cooldown_minutes = models.IntegerField(
        default=15, help_text=_("Minutes to wait before re-triggering notifications for the same alert")
    )
    recipients = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, help_text=_("Specific team members to notify.")
    )

    def __str__(self):
        return f"{self.name} ({self.device.name if self.device else 'Site: ' + self.site.name})"

    @property
    def metric_config(self):
        from apps.devices.datapoint_maps import effective_register_map

        register_map = effective_register_map(self.device) if self.device else {}
        config = register_map.get(self.telemetry_key, {})
        return config if isinstance(config, dict) else {}

    @property
    def metric_label(self):
        return self.metric_config.get("label") or self.telemetry_key.replace("_", " ").title()

    @property
    def metric_unit(self):
        unit = self.metric_config.get("unit", "")
        return {"degC": "°C", "degF": "°F"}.get(unit, unit)

    @property
    def threshold_display(self):
        condition_labels = {
            "gt": "above",
            "gte": "at or above",
            "lt": "below",
            "lte": "at or below",
            "eq": "equal to",
            "neq": "not equal to",
        }
        unit = f" {self.metric_unit}" if self.metric_unit else ""
        condition = condition_labels.get(self.condition, "outside the expected range")
        return f"{condition} {self.threshold:g}{unit}"


class Alert(BaseTeamModel):
    """A triggered alert instance."""

    STATUS_CHOICES = (
        ("active", _("Active")),
        ("acknowledged", _("Acknowledged")),
        ("resolved", _("Resolved")),
    )

    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="alerts")
    device = models.ForeignKey("devices.Device", on_delete=models.CASCADE, related_name="alerts")

    triggered_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    trigger_value = models.FloatField()  # The value that triggered the alert
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    notes = models.TextField(blank=True)

    @property
    def ticket(self):
        from apps.maintenance.models import MaintenanceTicket

        return MaintenanceTicket.objects.filter(alert_reference=str(self.id), team=self.team).first()

    @property
    def metric_config(self):
        return self.rule.metric_config

    @property
    def metric_label(self):
        return self.rule.metric_label

    @property
    def metric_unit(self):
        return self.rule.metric_unit

    @property
    def threshold_display(self):
        return self.rule.threshold_display

    @property
    def trigger_value_display(self):
        unit = f" {self.metric_unit}" if self.metric_unit else ""
        return f"{self.trigger_value:g}{unit}"

    @property
    def next_action_display(self):
        ticket = self.ticket
        if ticket:
            return f"Open TKT-{ticket.id}, confirm the device reading, then inspect or assign a contractor."
        if self.status == "resolved":
            return "Review the recovery and keep monitoring this device."
        return "Acknowledge the alert, then create a maintenance ticket if work is needed."

    def __str__(self):
        return f"{self.rule.name} on {self.device.name} @ {self.triggered_at}"
