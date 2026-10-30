from datetime import timedelta

from django import forms
from django.core.validators import FileExtensionValidator
from django.utils import timezone

from .models import MaintenanceTicket, PreventiveSchedule, SharedTicketLink, TicketComment, TicketTemplate


class MaintenanceTicketForm(forms.ModelForm):
    template = forms.ModelChoiceField(
        queryset=TicketTemplate.objects.none(),
        required=False,
        label="Checklist Template",
        help_text="Optionally load standard checklist tasks.",
    )

    class Meta:
        model = MaintenanceTicket
        fields = [
            "device",
            "template",
            "title",
            "description",
            "priority",
            "status",
            "assigned_to",
            "due_date",
            "downtime_minutes",
            "labor_minutes",
            "send_email_notification",
            "send_whatsapp_notification",
        ]
        widgets = {
            "due_date": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        cleaned_data = super().clean()
        send_email = cleaned_data.get("send_email_notification")
        send_whatsapp = cleaned_data.get("send_whatsapp_notification")

        assignee = cleaned_data.get("assigned_to")
        if (send_email or send_whatsapp) and not assignee:
            self.add_error("assigned_to", "Choose an assignee before selecting a notification channel.")
        if send_email and assignee and not assignee.email:
            self.add_error("send_email_notification", "This assignee has no email address.")
        if send_whatsapp and assignee and not getattr(assignee, "phone_number", None):
            self.add_error("send_whatsapp_notification", "This assignee has no WhatsApp number.")
        return cleaned_data

    def __init__(self, *args, **kwargs):
        team = kwargs.pop("team", None)
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.initial["send_email_notification"] = False
        self.fields["downtime_minutes"].required = False
        self.fields["labor_minutes"].required = False
        if team:
            self.fields["device"].queryset = self.fields["device"].queryset.filter(team=team)
            self.fields["template"].queryset = TicketTemplate.objects.filter(team=team)
            # Filter assigned_to to only team members
            from apps.users.models import CustomUser

            team_users = CustomUser.objects.filter(membership__team=team)
            self.fields["assigned_to"].queryset = team_users

    def save(self, commit=True):
        instance = super().save(commit=False)
        template = self.cleaned_data.get("template")
        if template and not instance.checklist_state:
            checklist_state = []
            for item in template.checklist:
                checklist_state.append(
                    {"task": item.get("task", ""), "required": item.get("required", False), "done": False}
                )
            instance.checklist_state = checklist_state
        if commit:
            instance.save()
        return instance


class TicketCommentForm(forms.ModelForm):
    attachment = forms.FileField(
        required=False,
        validators=[FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "pdf"])],
        widget=forms.FileInput(
            attrs={
                "accept": ".jpg,.jpeg,.png,.pdf",
                "class": "file-input file-input-bordered file-input-primary w-full",
            }
        ),
        help_text="Optional document or photo (JPG, PNG, PDF up to 10MB)",
    )

    class Meta:
        model = TicketComment
        fields = ["content", "attachment", "guest_name"]
        widgets = {
            "content": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": "Add a comment, upload certificates or signed checksheets...",
                    "class": "textarea textarea-bordered textarea-primary w-full bg-gray-900 text-white",
                }
            ),
            "guest_name": forms.TextInput(
                attrs={
                    "placeholder": "Enter your name (Required for guests)",
                    "class": "input input-bordered input-primary w-full bg-gray-900 text-white",
                }
            ),
        }

    def clean_attachment(self):
        attachment = self.cleaned_data.get("attachment")
        if attachment and attachment.size > 10 * 1024 * 1024:  # 10MB limit
            raise forms.ValidationError("Files must be under 10MB.")
        return attachment


class TicketTemplateForm(forms.ModelForm):
    checklist_steps = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        label="Checklist steps",
        help_text=(
            "One step per line. Prefix required steps with * (for example, * Inspect guard). "
            "Changes apply to future tickets only."
        ),
    )

    class Meta:
        model = TicketTemplate
        fields = ["name", "description", "estimated_duration_minutes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            self.initial["checklist_steps"] = "\n".join(
                ("* " if item.get("required") else "") + item.get("task", "") for item in self.instance.checklist
            )

    def clean_checklist_steps(self):
        steps = []
        for line_number, raw in enumerate(self.cleaned_data["checklist_steps"].splitlines(), 1):
            raw = raw.strip()
            if not raw:
                raise forms.ValidationError(f"Line {line_number} is empty. Remove it or enter a task.")
            required = raw.startswith("*")
            task = raw[1:].strip() if required else raw
            if not task:
                raise forms.ValidationError(f"Line {line_number} needs a task after *.")
            steps.append({"task": task, "required": required})
        return steps

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.checklist = self.cleaned_data["checklist_steps"]
        if commit:
            instance.save()
        return instance


class PreventiveScheduleForm(forms.ModelForm):
    class Meta:
        model = PreventiveSchedule
        fields = [
            "device",
            "template",
            "title",
            "interval",
            "next_due_at",
            "is_active",
            "is_usage_based",
            "usage_telemetry_key",
            "usage_threshold",
            "assigned_to",
            "send_email_notification",
            "send_whatsapp_notification",
        ]
        widgets = {
            "next_due_at": forms.DateInput(
                attrs={"type": "date", "class": "input input-bordered w-full bg-white dark:bg-gray-900"}
            ),
        }
        labels = {
            "next_due_at": "Next Due Date",
        }

    def __init__(self, *args, **kwargs):
        team = kwargs.pop("team", None)
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.initial["is_active"] = False
        if team:
            self.fields["device"].queryset = self.fields["device"].queryset.filter(team=team)
            self.fields["template"].queryset = self.fields["template"].queryset.filter(team=team)
            # Filter assigned_to to only team members
            from apps.users.models import CustomUser

            self.fields["assigned_to"].queryset = CustomUser.objects.filter(membership__team=team)
            from apps.devices.datapoint_maps import effective_register_map

            choices = [("", "Choose a mapped numeric reading")]
            seen = set()
            for device in self.fields["device"].queryset.select_related("template"):
                for key, config in effective_register_map(device).items():
                    if key in seen or not isinstance(config, dict):
                        continue
                    if (config.get("data_type") or config.get("type")) == "bool":
                        continue
                    choices.append((key, f"{key.replace('_', ' ').title()} ({config.get('unit') or 'units'})"))
                    seen.add(key)
            self.fields["usage_telemetry_key"].widget = forms.Select(choices=choices)

    def clean(self):
        cleaned_data = super().clean()
        assigned_to = cleaned_data.get("assigned_to")
        send_email = cleaned_data.get("send_email_notification")
        send_whatsapp = cleaned_data.get("send_whatsapp_notification")

        if assigned_to and not send_email and not send_whatsapp:
            raise forms.ValidationError(
                "You must select at least one notification channel (Email or WhatsApp) if assigning a user."
            )
        if cleaned_data.get("is_active"):
            if cleaned_data.get("is_usage_based"):
                if not cleaned_data.get("usage_telemetry_key"):
                    self.add_error("usage_telemetry_key", "Choose a mapped numeric reading.")
                if not cleaned_data.get("usage_threshold") or cleaned_data["usage_threshold"] <= 0:
                    self.add_error("usage_threshold", "Enter a positive usage increase.")
                device = cleaned_data.get("device")
                if device:
                    from apps.devices.datapoint_maps import effective_register_map

                    key = cleaned_data.get("usage_telemetry_key")
                    config = effective_register_map(device).get(key, {})
                    if not config or (config.get("data_type") or config.get("type")) == "bool":
                        self.add_error("usage_telemetry_key", "This reading is not mapped on the selected equipment.")
                    elif (
                        not self.instance.pk
                        or not self.instance.is_usage_based
                        or self.instance.usage_telemetry_key != key
                    ):
                        from apps.telemetry.models import TelemetryData

                        point = (
                            TelemetryData.objects.filter(
                                device=device,
                                key=key,
                                value_numeric__isnull=False,
                                timestamp__gte=timezone.now() - timedelta(minutes=5),
                            )
                            .order_by("-timestamp")
                            .first()
                        )
                        if not point:
                            self.add_error(
                                "usage_telemetry_key", "A fresh numeric baseline is needed before activation."
                            )
                        else:
                            self._usage_baseline = point.value_numeric
            else:
                if not cleaned_data.get("next_due_at"):
                    self.add_error("next_due_at", "Set the first due date before activating this calendar schedule.")
                if not cleaned_data.get("interval"):
                    self.add_error("interval", "Choose a recurrence interval.")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if hasattr(self, "_usage_baseline"):
            instance.last_trigger_usage_value = self._usage_baseline
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class SharedTicketLinkForm(forms.ModelForm):
    class Meta:
        model = SharedTicketLink
        fields = ["expires_at", "auto_revoke_on_resolve"]
        widgets = {
            "expires_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["expires_at"].required = True
        if not self.is_bound and not self.instance.pk:
            self.initial["expires_at"] = timezone.localtime(timezone.now() + timedelta(days=7)).strftime(
                "%Y-%m-%dT%H:%M"
            )

    def clean_expires_at(self):
        expires_at = self.cleaned_data["expires_at"]
        if expires_at <= timezone.now():
            raise forms.ValidationError("Choose a future expiry.")
        return expires_at
