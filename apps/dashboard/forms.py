from django import forms

from .models import SharedDashboard


class DateRangeForm(forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))


class SharedDashboardForm(forms.ModelForm):
    raw_password = forms.CharField(
        required=False,
        label="Password to open link",
        widget=forms.PasswordInput(render_value=True),
        help_text="Leave blank for no password. If set, users will need this password to view the dashboard.",
    )

    class Meta:
        model = SharedDashboard
        fields = ["name", "is_active", "expires_at"]
        widgets = {
            "expires_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].label = "Enable this public link"

    def save(self, commit=True):
        instance = super().save(commit=False)
        raw_pw = self.cleaned_data.get("raw_password")
        if raw_pw:
            instance.set_password(raw_pw)
        elif "raw_password" in self.changed_data:
            # If they cleared the field, remove password
            instance.set_password(None)

        if commit:
            instance.save()
        return instance
