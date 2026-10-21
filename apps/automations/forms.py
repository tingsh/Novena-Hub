from django import forms
from django.forms import inlineformset_factory

from .models import Automation, AutomationAction, AutomationCondition


class AutomationForm(forms.ModelForm):
    class Meta:
        model = Automation
        fields = ["name", "description", "is_active", "trigger_logic", "cooldown_minutes"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trigger_logic"].label = "When multiple conditions are used"
        self.fields["trigger_logic"].choices = (("and", "All conditions must match"), ("or", "Any condition may match"))
        self.fields["cooldown_minutes"].label = "Wait before this rule can run again (minutes)"


class AutomationConditionForm(forms.ModelForm):
    class Meta:
        model = AutomationCondition
        fields = ["device", "telemetry_key", "operator", "threshold", "duration_seconds"]

    def __init__(self, *args, **kwargs):
        team = kwargs.pop("team", None)
        super().__init__(*args, **kwargs)
        self.fields["telemetry_key"].label = "Reading to monitor"
        self.fields["operator"].label = "Comparison"
        self.fields["operator"].choices = (
            ("gt", "above"),
            ("lt", "below"),
            ("gte", "at or above"),
            ("lte", "at or below"),
            ("eq", "equal to"),
            ("neq", "not equal to"),
            ("is_true", "is on / true"),
            ("is_false", "is off / false"),
        )
        self.fields["threshold"].label = "Limit or expected value"
        self.fields["duration_seconds"].label = "Must remain true for (seconds)"
        if team:
            self.fields["device"].queryset = self.fields["device"].queryset.filter(team=team)


class AutomationActionForm(forms.ModelForm):
    class Meta:
        model = AutomationAction
        fields = [
            "action_type",
            "target_device",
            "command_key",
            "command_payload",
            "notify_emails",
            "webhook_url",
            "webhook_headers",
        ]

    def __init__(self, *args, **kwargs):
        team = kwargs.pop("team", None)
        super().__init__(*args, **kwargs)
        self.fields["action_type"].label = "What should Novena do?"
        self.fields["action_type"].choices = (
            ("", "Choose an action"),
            ("send_command", "Create an equipment action proposal"),
            ("notify_email", "Send an email notification"),
            ("webhook", "Notify an external system (technician setup)"),
            ("create_ticket", "Create a maintenance ticket"),
        )
        self.fields["target_device"].label = "Equipment"
        self.fields["command_key"].label = "Proposed equipment setting"
        self.fields["command_payload"].label = "Proposed value (advanced)"
        self.fields[
            "command_payload"
        ].help_text = "This records a proposal only. It does not automatically control equipment."
        self.fields["notify_emails"].label = "Email recipients"
        self.fields["webhook_url"].label = "External system URL"
        if team:
            self.fields["target_device"].queryset = self.fields["target_device"].queryset.filter(team=team)


# Formsets for the builder UI
ConditionFormSet = inlineformset_factory(
    Automation, AutomationCondition, form=AutomationConditionForm, extra=1, can_delete=True
)

ActionFormSet = inlineformset_factory(Automation, AutomationAction, form=AutomationActionForm, extra=1, can_delete=True)
