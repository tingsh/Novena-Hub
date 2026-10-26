from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.devices.models import Device
from apps.teams.models import BaseTeamModel


class Automation(BaseTeamModel):
    """The root entity defining a logic workflow."""

    class TriggerLogicChoices(models.TextChoices):
        AND = "and", _("All Conditions Match (AND)")
        OR = "or", _("Any Condition Matches (OR)")

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    trigger_logic = models.CharField(
        max_length=10, choices=TriggerLogicChoices.choices, default=TriggerLogicChoices.AND
    )
    cooldown_minutes = models.PositiveIntegerField(
        default=5, help_text=_("Minutes to wait after execution before this automation can fire again")
    )
    last_triggered_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class AutomationCondition(BaseTeamModel):
    """A rule that must be met to trigger the automation."""

    class OperatorChoices(models.TextChoices):
        GT = "gt", _("Greater Than (>)")
        LT = "lt", _("Less Than (<)")
        GTE = "gte", _("Greater Than or Equal (>=)")
        LTE = "lte", _("Less Than or Equal (<=)")
        EQ = "eq", _("Equal (==)")
        NEQ = "neq", _("Not Equal (!=)")
        IS_TRUE = "is_true", _("Is True")
        IS_FALSE = "is_false", _("Is False")

    automation = models.ForeignKey(Automation, on_delete=models.CASCADE, related_name="conditions")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="automation_conditions")

    telemetry_key = models.CharField(max_length=100)
    operator = models.CharField(max_length=20, choices=OperatorChoices.choices)
    threshold = models.CharField(
        max_length=255, blank=True, help_text=_("Can be a number, string, or left blank for boolean checks.")
    )

    duration_seconds = models.PositiveIntegerField(
        default=0, help_text=_("How long the condition must remain true before acting (sustained trigger)")
    )

    def __str__(self):
        return f"{self.device.name}.{self.telemetry_key} {self.operator} {self.threshold}"

    @property
    def reading_config(self):
        from apps.devices.datapoint_maps import effective_register_map

        config = effective_register_map(self.device).get(self.telemetry_key, {})
        return config if isinstance(config, dict) else {}

    @property
    def reading_label(self):
        return self.reading_config.get("label") or self.telemetry_key.replace("_", " ").title()

    @property
    def reading_unit(self):
        unit = self.reading_config.get("unit", "")
        return {"degC": "°C", "degF": "°F"}.get(unit, unit)

    @property
    def operator_label(self):
        return {
            "gt": "above",
            "lt": "below",
            "gte": "at or above",
            "lte": "at or below",
            "eq": "equal to",
            "neq": "not equal to",
            "is_true": "is on / true",
            "is_false": "is off / false",
        }.get(self.operator, "matches")


class AutomationAction(BaseTeamModel):
    """An action executed when the automation triggers."""

    class ActionTypeChoices(models.TextChoices):
        SEND_COMMAND = "send_command", _("Send Command to Device (RPC)")
        NOTIFY_EMAIL = "notify_email", _("Send Email Notification")
        WEBHOOK = "webhook", _("Fire HTTP Webhook")
        CREATE_TICKET = "create_ticket", _("Create Maintenance Ticket")

    automation = models.ForeignKey(Automation, on_delete=models.CASCADE, related_name="actions")
    action_type = models.CharField(max_length=20, choices=ActionTypeChoices.choices)

    # Target for SEND_COMMAND
    target_device = models.ForeignKey(
        Device, on_delete=models.CASCADE, null=True, blank=True, related_name="automation_actions"
    )
    command_key = models.CharField(max_length=100, blank=True)
    command_payload = models.JSONField(default=dict, blank=True, help_text=_("JSON wrapper for the command value"))

    # Target for NOTIFY_EMAIL
    notify_emails = models.CharField(max_length=255, blank=True, help_text=_("Comma separated list of email addresses"))
    email_body = models.TextField(blank=True)

    # Target for WEBHOOK
    webhook_url = models.URLField(blank=True)
    webhook_headers = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Action: {self.get_action_type_display()} for {self.automation.name}"


class AutomationLog(BaseTeamModel):
    """Audit trail of automation executions."""

    class StatusChoices(models.TextChoices):
        SUCCESS = "success", _("Success")
        PARTIAL = "partial", _("Partial Success")
        FAILED = "failed", _("Failed")

    automation = models.ForeignKey(Automation, on_delete=models.CASCADE, related_name="logs")
    triggered_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=StatusChoices.choices)
    details = models.TextField(blank=True)

    def __str__(self):
        return f"{self.automation.name} execution at {self.triggered_at} ({self.status})"
