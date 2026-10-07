from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse

from apps.alerts.forms import AlertRuleForm
from apps.alerts.models import Alert, AlertRule
from apps.alerts.services import check_alerts_for_payload
from apps.alerts.tasks import dispatch_alert_whatsapp_task
from apps.devices.models import Device, Site
from apps.events.models import EmailDelivery
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_ADMIN
from apps.users.models import CustomUser


@pytest.mark.django_db
def test_alert_rule_form_requires_recipients_when_email_enabled():
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )

    form = AlertRuleForm(
        team=team,
        data={
            "name": "No recipient rule",
            "device": device.id,
            "site": site.id,
            "telemetry_key": "temp",
            "condition": "gt",
            "threshold": "50",
            "severity": "warning",
            "is_active": "on",
            "notify_email": "on",
            "cooldown_minutes": "15",
            "duration_seconds": "0",
        },
    )

    assert not form.is_valid()
    assert "recipients" in form.errors


@pytest.mark.django_db
def test_alert_rule_form_can_disable_rule_without_recipient():
    team = Team.objects.create(name="Disable Team", slug="disable-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(team=team, site=site, name="Test Device")
    form = AlertRuleForm(
        team=team,
        data={
            "name": "Legacy alert",
            "device": device.id,
            "site": site.id,
            "telemetry_key": "active_power",
            "condition": "gt",
            "threshold": "1200",
            "severity": "warning",
            "notify_email": "on",
            "cooldown_minutes": "15",
            "duration_seconds": "0",
        },
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["is_active"] is False


@pytest.mark.django_db
def test_alert_rule_form_uses_operator_labels():
    team = Team.objects.create(name="Labels", slug="labels")
    form = AlertRuleForm(team=team)

    assert form.fields["telemetry_key"].label == "Reading to monitor"
    assert form.fields["condition"].label == "Alert when the reading is"
    assert dict(form.fields["condition"].choices)["gt"] == "above"


@pytest.mark.django_db
def test_alert_rule_display_translates_signal_and_condition_codes():
    team = Team.objects.create(name="Display", slug="display")
    site = Site.objects.create(team=team, name="Cold Store")
    device = Device.objects.create(team=team, site=site, name="Cold Room")
    rule = AlertRule.objects.create(
        team=team,
        device=device,
        name="High temperature",
        telemetry_key="cold_room_temperature",
        condition="gt",
        threshold=8,
    )

    assert rule.metric_label == "Cold Room Temperature"
    assert rule.threshold_display == "above 8"


@pytest.mark.django_db
def test_onboarding_alert_rule_adds_current_user_as_email_recipient():
    client = Client()
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )
    user = CustomUser.objects.create_user(username="admin", email="admin@example.com", password="pwd")
    Membership.objects.create(team=team, user=user, role=ROLE_ADMIN)
    client.force_login(user)
    session = client.session
    session["onboarding_device_id"] = device.id
    session.save()

    response = client.post(
        reverse("web_team:onboarding:step_4_alert", args=[team.slug]),
        {"accept_profile_presets": "0", "key": "temp", "threshold": "45"},
    )

    assert response.status_code == 302
    rule = AlertRule.objects.get(device=device, telemetry_key="temp")
    assert rule.notify_email is True
    assert rule.recipients.filter(id=user.id).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="http://testserver", CELERY_TASK_ALWAYS_EAGER=True)
def test_check_alerts_for_payload_triggers_alert_and_email():
    # 1. Create a Team
    team = Team.objects.create(name="Test Team", slug="test-team")

    # 2. Create a Site
    site = Site.objects.create(team=team, name="Test Site")

    # 3. Create a Device
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )

    # 4. Create a user to receive the alert email
    user = CustomUser.objects.create_user(username="admin_test", email="admin@test.com", password="testpassword123")
    # Associate user with team
    team.members.add(user)

    # 5. Create an AlertRule (threshold=100, condition='gt')
    rule = AlertRule.objects.create(
        team=team,
        name="High Active Power Alert",
        device=device,
        telemetry_key="active_power",
        condition="gt",
        threshold=100.0,
        is_active=True,
        notify_email=True,
        cooldown_minutes=15,
    )
    rule.recipients.add(user)

    # 6. Trigger alert with payload 105 (greater than 100)
    check_alerts_for_payload(device, "active_power", 105.0)

    # 7. Asserts
    # Assert Alert instance was created
    alerts = Alert.objects.filter(rule=rule, device=device)
    assert alerts.count() == 1
    alert = alerts.first()
    assert alert.trigger_value == 105.0
    assert alert.status == "active"

    # Assert email was sent
    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert "High Active Power Alert" in email.subject
    assert "admin@test.com" in email.to
    delivery = EmailDelivery.objects.get(alert=alert, recipient="admin@test.com")
    assert delivery.notification_type == EmailDelivery.NotificationType.ALERT_TRIGGERED
    assert delivery.status == EmailDelivery.Status.SENT
    assert delivery.attempt_count == 1


@pytest.mark.django_db
@override_settings(SITE_URL="http://testserver", CELERY_TASK_ALWAYS_EAGER=True)
def test_targeted_recipients_email_dispatch():
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )

    user1 = CustomUser.objects.create_user(username="user1", email="user1@test.com", password="pwd")
    user2 = CustomUser.objects.create_user(username="user2", email="user2@test.com", password="pwd")
    team.members.add(user1)
    team.members.add(user2)

    rule = AlertRule.objects.create(
        team=team,
        name="Targeted Email Alert",
        device=device,
        telemetry_key="temp",
        condition="gt",
        threshold=50.0,
        is_active=True,
        notify_email=True,
        cooldown_minutes=15,
    )
    rule.recipients.add(user1)  # Only user1 is recipient

    check_alerts_for_payload(device, "temp", 55.0)

    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert "user1@test.com" in email.to
    assert "user2@test.com" not in email.to


@pytest.mark.django_db
@override_settings(
    WHATSAPP_PROVIDER="meta",
    WHATSAPP_GRAPH_API_VERSION="v21.0",
    WHATSAPP_PHONE_NUMBER_ID="12345",
    WHATSAPP_ACCESS_TOKEN="token_abc",
    WHATSAPP_ALERT_TEMPLATE_NAME="novena_alert_notification",
    WHATSAPP_ALERT_TEMPLATE_LANGUAGE="en",
)
@patch("apps.alerts.whatsapp.requests.post")
def test_targeted_whatsapp_dispatch(mock_post):
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site", timezone="Australia/Sydney")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )

    user = CustomUser.objects.create_user(
        username="user1", email="user1@test.com", password="pwd", phone_number="+1 (555) 123-4567"
    )
    team.members.add(user)

    rule = AlertRule.objects.create(
        team=team,
        name="Targeted WhatsApp Alert",
        device=device,
        telemetry_key="temp",
        condition="gt",
        threshold=50.0,
        is_active=True,
        notify_whatsapp=True,
        cooldown_minutes=15,
    )
    rule.recipients.add(user)

    alert = Alert.objects.create(team=team, rule=rule, device=device, trigger_value=55.0, status="active")
    Alert.objects.filter(id=alert.id).update(triggered_at=datetime(2026, 7, 13, 2, 42, 25, tzinfo=UTC))

    dispatch_alert_whatsapp_task(alert.id)

    assert mock_post.called
    args, kwargs = mock_post.call_args
    assert args[0] == "https://graph.facebook.com/v21.0/12345/messages"
    assert kwargs["headers"]["Authorization"] == "Bearer token_abc"
    payload = kwargs["json"]
    assert payload["to"] == "15551234567"
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "novena_alert_notification"
    assert payload["template"]["language"]["code"] == "en"
    header_parameters = payload["template"]["components"][0]["parameters"]
    body_parameters = payload["template"]["components"][1]["parameters"]
    assert payload["template"]["components"][0]["type"] == "header"
    assert payload["template"]["components"][1]["type"] == "body"
    assert [param["type"] for param in header_parameters] == ["text"]
    assert [param["type"] for param in body_parameters] == ["text"] * 4
    assert header_parameters[0]["text"] == "Warning"
    assert body_parameters[0]["text"] == "Targeted WhatsApp Alert"
    assert body_parameters[1]["text"] == "Test Device"
    assert body_parameters[2]["text"] == "55.0"
    assert body_parameters[3]["text"] == "2026-07-13 12:42:25 AEST"


@pytest.mark.django_db
def test_search_team_members_htmx():
    client = Client()
    team = Team.objects.create(name="Test Team", slug="test-team")
    user = CustomUser.objects.create_user(
        username="john_doe", first_name="John", last_name="Doe", email="john@test.com", password="pwd"
    )
    Membership.objects.create(team=team, user=user, role=ROLE_ADMIN)

    # Login user to pass PermissionRequiredMixin
    client.force_login(user)

    # Test search with name
    url = reverse("web_team:alerts:search_users", args=[team.slug])
    response = client.get(url, {"q": "Joh"})
    assert response.status_code == 200
    assert b"John Doe" in response.content

    # Test search with no matching letters
    response = client.get(url, {"q": "XYZ"})
    assert response.status_code == 200
    assert b"John Doe" not in response.content


@pytest.mark.django_db
@override_settings(SITE_URL="http://testserver", CELERY_TASK_ALWAYS_EAGER=True)
def test_alert_auto_resolution():
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )
    user = CustomUser.objects.create_user(username="test_user", email="test@test.com", password="pwd")
    team.members.add(user)

    rule = AlertRule.objects.create(
        team=team,
        name="Boiler Temp Alert",
        device=device,
        telemetry_key="temp",
        condition="gt",
        threshold=100.0,
        is_active=True,
        notify_email=True,
        cooldown_minutes=15,
    )
    rule.recipients.add(user)

    # 1. Trigger the alert
    check_alerts_for_payload(device, "temp", 105.0)
    alerts = Alert.objects.filter(rule=rule, device=device)
    assert alerts.count() == 1
    alert = alerts.first()
    assert alert.status == "active"

    # 2. Reset outbox
    mail.outbox.clear()

    # 3. Telemetry goes back to normal (e.g. 95.0) -> resolves alert
    check_alerts_for_payload(device, "temp", 95.0)
    alert.refresh_from_db()
    assert alert.status == "resolved"
    assert alert.resolved_at is not None

    # Assert resolved email was sent
    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert "[RESOLVED]" in email.subject
    assert "test@test.com" in email.to


@pytest.mark.django_db
def test_device_specific_alert_rule_does_not_trigger_for_other_device_at_same_site():
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    power_meter = Device.objects.create(
        team=team,
        site=site,
        name="Power Meter",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="online",
    )
    chiller = Device.objects.create(
        team=team,
        site=site,
        name="Chiller",
        device_type="chiller",
        protocol="modbus_tcp",
        status="online",
    )
    rule = AlertRule.objects.create(
        team=team,
        name="Power Meter Spike",
        device=power_meter,
        site=site,
        telemetry_key="active_power",
        condition="gt",
        threshold=1200.0,
        is_active=True,
        notify_email=False,
        notify_whatsapp=False,
    )

    check_alerts_for_payload(chiller, "active_power", 1500.0)

    assert Alert.objects.filter(rule=rule).count() == 0


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
def test_cooldown_hardening():
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )
    user = CustomUser.objects.create_user(username="test_user", email="test@test.com", password="pwd")
    team.members.add(user)

    rule = AlertRule.objects.create(
        team=team,
        name="Voltage Alert",
        device=device,
        telemetry_key="voltage",
        condition="gt",
        threshold=240.0,
        is_active=True,
        notify_email=True,
        cooldown_minutes=15,
    )
    rule.recipients.add(user)

    # 1. Trigger the alert
    check_alerts_for_payload(device, "voltage", 245.0)
    assert Alert.objects.filter(rule=rule, device=device).count() == 1
    alert = Alert.objects.filter(rule=rule, device=device).first()
    assert alert.status == "active"

    # 2. Acknowledge the alert
    alert.status = "acknowledged"
    alert.save()

    # 3. Send out-of-bounds telemetry again. Should not trigger a duplicate alert since cooldown is active.
    check_alerts_for_payload(device, "voltage", 246.0)
    assert Alert.objects.filter(rule=rule, device=device).count() == 1  # Still just 1 alert


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
def test_duration_seconds_evaluation():
    from datetime import timedelta

    from django.utils import timezone

    from apps.telemetry.models import TelemetryData

    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )
    user = CustomUser.objects.create_user(username="test_user", email="test@test.com", password="pwd")
    team.members.add(user)

    rule = AlertRule.objects.create(
        team=team,
        name="Persistent Current Alert",
        device=device,
        telemetry_key="current",
        condition="gt",
        threshold=10.0,
        is_active=True,
        notify_email=True,
        cooldown_minutes=15,
        duration_seconds=30,  # Needs to exceed for 30s
    )
    rule.recipients.add(user)

    # 1. First out-of-bounds point. No history in window, shouldn't trigger yet.
    TelemetryData.objects.create(device=device, key="current", value_numeric=12.0, timestamp=timezone.now())
    check_alerts_for_payload(device, "current", 12.0)
    assert Alert.objects.filter(rule=rule, device=device).count() == 0

    # 2. Add points spanning the 30s window
    now = timezone.now()
    TelemetryData.objects.create(
        device=device, key="current", value_numeric=11.5, timestamp=now - timedelta(seconds=20)
    )
    TelemetryData.objects.create(
        device=device, key="current", value_numeric=11.8, timestamp=now - timedelta(seconds=35)
    )

    check_alerts_for_payload(device, "current", 12.0)
    assert Alert.objects.filter(rule=rule, device=device).count() == 1


@pytest.mark.django_db
@patch("apps.alerts.tasks.requests.post")
def test_async_webhook_dispatch(mock_post):
    from apps.alerts.tasks import dispatch_alert_webhook_task

    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )

    rule = AlertRule.objects.create(
        team=team,
        name="Webhook Alert",
        device=device,
        telemetry_key="temp",
        condition="gt",
        threshold=50.0,
        is_active=True,
        notify_webhook="https://mywebhooks.com/receiver",
        cooldown_minutes=15,
    )

    alert = Alert.objects.create(team=team, rule=rule, device=device, trigger_value=55.0, status="active")

    dispatch_alert_webhook_task(alert.id, is_resolved=False)

    assert mock_post.called
    args, kwargs = mock_post.call_args
    assert args[0] == "https://mywebhooks.com/receiver"
    payload = kwargs["json"]
    assert payload["alert_id"] == alert.id
    assert payload["status"] == "active"


@pytest.mark.django_db
def test_manual_escalate_alert_success():
    client = Client()
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )
    user = CustomUser.objects.create_user(username="m", email="m@test.com", password="pwd")
    Membership.objects.create(team=team, user=user, role="admin")

    rule = AlertRule.objects.create(
        team=team,
        name="Escalation Test Rule",
        device=device,
        telemetry_key="temp",
        condition="gt",
        threshold=50.0,
        create_maintenance_ticket=False,
        is_active=True,
    )
    alert = Alert.objects.create(team=team, rule=rule, device=device, trigger_value=55.0, status="active")

    client.force_login(user)
    url = reverse("web_team:alerts:escalate_alert", args=[team.slug, alert.id])

    response = client.post(url)
    assert response.status_code == 200

    ticket = alert.ticket
    assert ticket is not None
    assert ticket.ticket_type == "reactive"
    assert "Escalation Test Rule" in ticket.title
    assert b"TKT-" in response.content


@pytest.mark.django_db
def test_manual_escalate_alert_permission_denied():
    client = Client()
    team = Team.objects.create(name="Test Team", slug="test-team")
    site = Site.objects.create(team=team, name="Test Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )
    user = CustomUser.objects.create_user(username="v", email="v@test.com", password="pwd")
    from apps.teams.roles import ROLE_VIEWER

    Membership.objects.create(team=team, user=user, role=ROLE_VIEWER)

    rule = AlertRule.objects.create(
        team=team,
        name="Escalation Test Rule",
        device=device,
        telemetry_key="temp",
        condition="gt",
        threshold=50.0,
        create_maintenance_ticket=False,
        is_active=True,
    )
    alert = Alert.objects.create(team=team, rule=rule, device=device, trigger_value=55.0, status="active")

    client.force_login(user)
    url = reverse("web_team:alerts:escalate_alert", args=[team.slug, alert.id])

    response = client.post(url)
    assert response.status_code == 403
    assert alert.ticket is None
