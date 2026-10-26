from django import forms
from django.core.exceptions import ValidationError

from apps.devices.datapoint_maps import effective_register_map
from apps.devices.models import Device, Site
from apps.maintenance.models import TicketTemplate
from apps.users.models import CustomUser

from .models import AlertRule


def _display_unit(unit):
    return {"degC": "°C", "degF": "°F"}.get(unit, unit or "")


class AlertRuleForm(forms.ModelForm):
    class Meta:
        model = AlertRule
        fields = [
            "name",
            "device",
            "site",
            "telemetry_key",
            "condition",
            "threshold",
            "severity",
            "is_active",
            "notify_email",
            "notify_whatsapp",
            "notify_webhook",
            "cooldown_minutes",
            "duration_seconds",
            "recipients",
            "create_maintenance_ticket",
            "maintenance_template",
        ]

    def __init__(self, *args, **kwargs):
        team = kwargs.pop("team", None)
        super().__init__(*args, **kwargs)
        self.available_signals = []
        labels = {
            "name": "Alert name",
            "device": "Equipment",
            "site": "Site",
            "telemetry_key": "Reading to monitor",
            "condition": "Alert when the reading is",
            "threshold": "Limit",
            "severity": "Urgency",
            "duration_seconds": "How long the limit must be exceeded (seconds)",
            "cooldown_minutes": "Wait before sending another notification (minutes)",
            "recipients": "People to notify",
            "create_maintenance_ticket": "Create a maintenance ticket when triggered",
            "maintenance_template": "Maintenance checklist",
        }
        for field_name, label in labels.items():
            self.fields[field_name].label = label
        self.fields["condition"].choices = (
            ("gt", "above"),
            ("gte", "at or above"),
            ("lt", "below"),
            ("lte", "at or below"),
            ("eq", "equal to"),
            ("neq", "not equal to"),
        )
        if team:
            devices = Device.objects.filter(team=team).select_related("site", "template")
            self.fields["device"].queryset = devices
            self.fields["site"].queryset = Site.objects.filter(team=team)
            self.fields["maintenance_template"].queryset = TicketTemplate.objects.filter(team=team)
            self.fields["recipients"].queryset = CustomUser.objects.filter(membership__team=team)
            selected_device = self.data.get("device") if self.is_bound else self.initial.get("device")
            selected_device_id = str(getattr(selected_device, "pk", selected_device) or self.instance.device_id or "")
            selected_site = self.data.get("site") if self.is_bound else self.initial.get("site")
            selected_site_id = str(getattr(selected_site, "pk", selected_site) or self.instance.site_id or "")
            selected_key = self.data.get("telemetry_key") if self.is_bound else self.initial.get("telemetry_key")
            selected_key = str(selected_key or self.instance.telemetry_key or "")
            selected_found = False
            site_key_units = {}
            for device in devices:
                for key, config in effective_register_map(device).items():
                    if not isinstance(config, dict):
                        continue
                    selected = key == selected_key and (not selected_device_id or str(device.pk) == selected_device_id)
                    if selected and not selected_found:
                        selected_found = True
                    else:
                        selected = False
                    unit = _display_unit(config.get("unit"))
                    site_key_units.setdefault((device.site_id, key), set()).add(unit)
                    self.available_signals.append(
                        {
                            "device_id": device.pk,
                            "site_id": device.site_id,
                            "key": key,
                            "label": config.get("label") or key.replace("_", " ").title(),
                            "unit": unit,
                            "device_name": device.name,
                            "selected": selected,
                        }
                    )
            for signal in self.available_signals:
                signal["site_wide_safe"] = len(site_key_units[(signal["site_id"], signal["key"])]) == 1
            if selected_key and not selected_found:
                self.available_signals.append(
                    {
                        "device_id": selected_device_id,
                        "site_id": selected_site_id,
                        "key": selected_key,
                        "label": f"Previously saved: {selected_key} (unverified)",
                        "unit": "",
                        "device_name": "",
                        "selected": True,
                        "site_wide_safe": False,
                    }
                )

    def clean(self):
        cleaned_data = super().clean()
        device = cleaned_data.get("device")
        site = cleaned_data.get("site")
        if device and site and device.site_id != site.pk:
            self.add_error("site", "Choose the site that contains this equipment.")
        if not device and not site:
            self.add_error("device", "Choose equipment or a site to monitor.")
        if cleaned_data.get("is_active") and (device or site):
            devices = [device] if device else Device.objects.filter(team=site.team, site=site)
            chosen_key = cleaned_data.get("telemetry_key")
            configs = [effective_register_map(item).get(chosen_key) for item in devices]
            mapped_configs = [config for config in configs if isinstance(config, dict)]
            if not mapped_configs:
                self.add_error(
                    "telemetry_key",
                    "Choose a reading from the selected equipment's saved mapping before enabling this rule.",
                )
            elif not device and len({_display_unit(config.get("unit")) for config in mapped_configs}) > 1:
                self.add_error(
                    "telemetry_key",
                    "This reading uses different units across the site. Choose one equipment for this rule.",
                )
        notify_email = cleaned_data.get("notify_email")
        notify_whatsapp = cleaned_data.get("notify_whatsapp")
        recipients = cleaned_data.get("recipients")
        if cleaned_data.get("is_active") and (notify_email or notify_whatsapp) and not recipients:
            raise ValidationError({"recipients": "Select at least one recipient when notifications are enabled."})
        return cleaned_data
