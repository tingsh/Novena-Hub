import math

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.forms import inlineformset_factory

from apps.devices.datapoint_maps import effective_register_map
from apps.devices.models import Device

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
        self.signal_options = []
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
        self.fields["operator"].widget.attrs["data-condition-operator"] = ""
        self.fields["threshold"].label = "Limit or expected value"
        self.fields[
            "threshold"
        ].help_text = "Enter a number for measured readings. Status readings use on/off comparisons."
        self.fields["duration_seconds"].label = "Must remain true for (seconds)"
        if team:
            devices = Device.objects.filter(team=team).select_related("template")
            self.fields["device"].queryset = devices
            selected_device = self.data.get(self.add_prefix("device")) if self.is_bound else self.initial.get("device")
            selected_device_id = str(getattr(selected_device, "pk", selected_device) or self.instance.device_id or "")
            selected_key = (
                self.data.get(self.add_prefix("telemetry_key")) if self.is_bound else self.initial.get("telemetry_key")
            )
            selected_key = str(selected_key or self.instance.telemetry_key or "")
            selected_found = False
            for device in devices:
                for key, config in effective_register_map(device).items():
                    if not isinstance(config, dict):
                        continue
                    selected = str(device.pk) == selected_device_id and key == selected_key
                    selected_found = selected_found or selected
                    unit = {"degC": "°C", "degF": "°F"}.get(config.get("unit"), config.get("unit") or "")
                    label = config.get("label") or key.replace("_", " ").title()
                    self.signal_options.append(
                        {
                            "device_id": device.pk,
                            "key": key,
                            "label": f"{label} ({unit})" if unit else label,
                            "is_boolean": config.get("type") in {"bool", "boolean"},
                            "selected": selected,
                        }
                    )
            if selected_key and not selected_found:
                self.signal_options.append(
                    {
                        "device_id": selected_device_id,
                        "key": selected_key,
                        "label": f"Previously saved: {selected_key} (unverified)",
                        "selected": True,
                    }
                )

    def clean(self):
        cleaned_data = super().clean()
        device = cleaned_data.get("device")
        key = cleaned_data.get("telemetry_key")
        if device and key and key not in effective_register_map(device):
            self.add_error("telemetry_key", "Choose a reading from this equipment's saved mapping.")
        if device and key and key in effective_register_map(device):
            config = effective_register_map(device)[key]
            is_boolean = isinstance(config, dict) and config.get("type") in {"bool", "boolean"}
            bool_operator = cleaned_data.get("operator") in {"is_true", "is_false"}
            if is_boolean != bool_operator:
                self.add_error(
                    "operator",
                    "Use on/off comparisons for a status reading, or a numeric comparison for a measured value.",
                )
        if cleaned_data.get("operator") in {"gt", "lt", "gte", "lte"}:
            try:
                limit = float(cleaned_data.get("threshold") or "")
            except (TypeError, ValueError):
                self.add_error("threshold", "Enter a numeric limit for this comparison.")
            else:
                if not math.isfinite(limit):
                    self.add_error("threshold", "Enter a finite numeric limit.")
        return cleaned_data

    def has_changed(self):
        # Newly added rows contain default comparison/duration values even when
        # the operator has not chosen an equipment reading yet.
        if self.is_bound and not self.instance.pk:
            meaningful = ("device", "telemetry_key", "threshold")
            if not any(str(self.data.get(self.add_prefix(field)) or "").strip() for field in meaningful):
                return False
        return super().has_changed()


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
        self.fields["action_type"].widget.attrs["x-model"] = "actionType"
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

    def clean(self):
        cleaned_data = super().clean()
        action_type = cleaned_data.get("action_type")
        if action_type in {"send_command", "create_ticket"} and not cleaned_data.get("target_device"):
            self.add_error("target_device", "Choose the equipment for this action.")
        if action_type == "send_command" and not cleaned_data.get("command_key"):
            self.add_error("command_key", "Choose the proposed equipment setting.")
        if action_type == "notify_email":
            recipients = [email.strip().lower() for email in (cleaned_data.get("notify_emails") or "").split(",")]
            recipients = list(dict.fromkeys(email for email in recipients if email))
            if not recipients:
                self.add_error("notify_emails", "Enter at least one email recipient.")
            else:
                for email in recipients:
                    try:
                        validate_email(email)
                    except ValidationError:
                        self.add_error("notify_emails", f"Enter a valid email address: {email}.")
                cleaned_data["notify_emails"] = ", ".join(recipients)
        if action_type == "webhook" and not cleaned_data.get("webhook_url"):
            self.add_error("webhook_url", "Enter the external system URL.")
        return cleaned_data


# Formsets for the builder UI
ConditionFormSet = inlineformset_factory(
    Automation, AutomationCondition, form=AutomationConditionForm, extra=1, can_delete=True
)

ActionFormSet = inlineformset_factory(Automation, AutomationAction, form=AutomationActionForm, extra=1, can_delete=True)
