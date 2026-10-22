from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.devices.models import Device, DeviceTemplate, Site
from apps.events.models import EmailDelivery
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_ADMIN
from apps.users.models import CustomUser

from .engine import evaluate_automations, evaluate_condition, execute_automation
from .forms import AutomationActionForm, AutomationConditionForm
from .models import Automation, AutomationAction, AutomationCondition, AutomationLog
from .tasks import dispatch_automation_email_task


class AutomationsEngineTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Stark Industries", slug="stark")
        self.site = Site.objects.create(team=self.team, name="HQ")
        self.device = Device.objects.create(team=self.team, site=self.site, name="Pump Station")

        self.automation = Automation.objects.create(
            team=self.team,
            name="High Temp Alert",
            trigger_logic="and",
            cooldown_minutes=0,  # Easy for testing
        )

    def test_evaluate_condition_gt(self):
        condition = AutomationCondition.objects.create(
            team=self.team,
            automation=self.automation,
            device=self.device,
            telemetry_key="temp",
            operator="gt",
            threshold="100.0",
        )
        # Should be met
        self.assertTrue(evaluate_condition(condition, 105.0))
        # Should not be met
        self.assertFalse(evaluate_condition(condition, 99.0))
        self.assertEqual(condition.reading_label, "Temp")
        self.assertEqual(condition.operator_label, "above")

    def test_equipment_action_is_labelled_as_a_proposal(self):
        form = AutomationActionForm(team=self.team)
        choices = dict(form.fields["action_type"].choices)

        self.assertEqual(choices["send_command"], "Create an equipment action proposal")
        self.assertIn("does not automatically control equipment", form.fields["command_payload"].help_text)

    def test_condition_requires_a_saved_mapped_reading(self):
        template = DeviceTemplate.objects.create(
            name="Cold Sensor", device_type="temperature_sensor", protocol="modbus_tcp",
            register_map={"temperature": {"address": 0, "type": "float32", "unit": "degC"}},
        )
        self.device.template = template
        self.device.save(update_fields=["template"])
        data = {
            "device": self.device.pk, "telemetry_key": "temperatur", "operator": "gt",
            "threshold": "4", "duration_seconds": "60",
        }
        form = AutomationConditionForm(team=self.team, data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("telemetry_key", form.errors)
        self.assertEqual(form.signal_options[0]["label"], "Temperature (°C)")

        data["telemetry_key"] = "temperature"
        form = AutomationConditionForm(team=self.team, data=data)
        self.assertTrue(form.is_valid(), form.errors)
        condition = AutomationCondition.objects.create(
            team=self.team, automation=self.automation, device=self.device,
            telemetry_key="temperature", operator="gt", threshold="4",
        )
        self.assertEqual(condition.reading_label, "Temperature")
        self.assertEqual(condition.reading_unit, "°C")

        data["threshold"] = ""
        form = AutomationConditionForm(team=self.team, data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("threshold", form.errors)

    def test_unfilled_added_condition_does_not_block_saving(self):
        form = AutomationConditionForm(
            team=self.team, prefix="conditions-1", empty_permitted=True, use_required_attribute=False,
            data={
                "conditions-1-device": "", "conditions-1-telemetry_key": "",
                "conditions-1-operator": "gt", "conditions-1-threshold": "",
                "conditions-1-duration_seconds": "0",
            },
        )
        self.assertFalse(form.has_changed())
        self.assertTrue(form.is_valid(), form.errors)

    def test_status_readings_require_on_off_comparisons(self):
        template = DeviceTemplate.objects.create(
            name="Cold Status", device_type="temperature_sensor", protocol="modbus_tcp",
            register_map={"door_open": {"address": 10, "type": "bool"}},
        )
        self.device.template = template
        self.device.save(update_fields=["template"])
        data = {
            "device": self.device.pk, "telemetry_key": "door_open", "operator": "gt",
            "threshold": "1", "duration_seconds": "0",
        }
        form = AutomationConditionForm(team=self.team, data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("operator", form.errors)
        self.assertTrue(form.signal_options[0]["is_boolean"])

        data["operator"] = "is_true"
        data["threshold"] = ""
        form = AutomationConditionForm(team=self.team, data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_action_requires_a_recipient_or_equipment_for_selected_type(self):
        email = AutomationActionForm(team=self.team, data={"action_type": "notify_email"})
        self.assertFalse(email.is_valid())
        self.assertIn("notify_emails", email.errors)
        ticket = AutomationActionForm(team=self.team, data={"action_type": "create_ticket"})
        self.assertFalse(ticket.is_valid())
        self.assertIn("target_device", ticket.errors)
        invalid_email = AutomationActionForm(
            team=self.team, data={"action_type": "notify_email", "notify_emails": "not-an-email"}
        )
        self.assertFalse(invalid_email.is_valid())
        self.assertIn("notify_emails", invalid_email.errors)

    @patch("apps.automations.tasks.dispatch_automation_email_task.delay")
    def test_email_action_is_queued_not_reported_as_sent(self, delay):
        action = AutomationAction.objects.create(
            team=self.team, automation=self.automation,
            action_type="notify_email", notify_emails="ops@example.com",
        )
        execute_automation(self.automation, trigger_device=self.device)
        self.assertEqual(delay.call_args.args[0], action.pk)
        self.assertEqual(delay.call_args.args[2], self.device.pk)
        self.assertIn("Email queued", self.automation.logs.get().details)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_automation_email_is_tracked_and_idempotent(self):
        action = AutomationAction.objects.create(
            team=self.team, automation=self.automation,
            action_type="notify_email", notify_emails="ops@example.com",
        )
        dispatch_automation_email_task(action.pk, "test-event-1", self.device.pk)
        delivery = EmailDelivery.objects.get(
            team=self.team, notification_type=EmailDelivery.NotificationType.AUTOMATION
        )
        self.assertEqual(delivery.status, EmailDelivery.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.device.name, mail.outbox[0].body)

        dispatch_automation_email_task(action.pk, "test-event-1", self.device.pk)
        self.assertEqual(len(mail.outbox), 1)

    def test_customer_builder_saves_mapped_condition_and_action_with_team(self):
        template = DeviceTemplate.objects.create(
            name="Cold Sensor", device_type="temperature_sensor", protocol="modbus_tcp",
            register_map={"temperature": {"address": 0, "type": "float32", "unit": "degC"}},
        )
        self.device.template = template
        self.device.save(update_fields=["template"])
        user = CustomUser.objects.create_user(username="builder", email="builder@example.com", password="pwd")
        Membership.objects.create(team=self.team, user=user, role=ROLE_ADMIN)
        client = Client()
        client.force_login(user)
        payload = {
            "name": "Warm room ticket", "is_active": "on", "trigger_logic": "and", "cooldown_minutes": "5",
            "conditions-TOTAL_FORMS": "1", "conditions-INITIAL_FORMS": "0",
            "conditions-MIN_NUM_FORMS": "0", "conditions-MAX_NUM_FORMS": "1000",
            "conditions-0-device": str(self.device.pk), "conditions-0-telemetry_key": "temperature",
            "conditions-0-operator": "gt", "conditions-0-threshold": "4", "conditions-0-duration_seconds": "60",
            "actions-TOTAL_FORMS": "1", "actions-INITIAL_FORMS": "0",
            "actions-MIN_NUM_FORMS": "0", "actions-MAX_NUM_FORMS": "1000",
            "actions-0-action_type": "create_ticket", "actions-0-target_device": str(self.device.pk),
        }
        url = reverse("web_team:automations:create", args=[self.team.slug])
        response = client.post(url, payload)
        self.assertEqual(response.status_code, 302, response.content.decode()[:1000])
        created = Automation.objects.get(team=self.team, name="Warm room ticket")
        self.assertEqual(created.conditions.get().team, self.team)
        self.assertEqual(created.actions.get().team, self.team)

        payload["conditions-0-telemetry_key"] = "temperatur"
        payload["name"] = "Typo must not save"
        response = client.post(url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a reading from this equipment")
        self.assertFalse(Automation.objects.filter(team=self.team, name="Typo must not save").exists())

    def test_evaluate_condition_is_true(self):
        condition = AutomationCondition.objects.create(
            team=self.team,
            automation=self.automation,
            device=self.device,
            telemetry_key="motor_fault",
            operator="is_true",
        )
        self.assertTrue(evaluate_condition(condition, True))
        self.assertFalse(evaluate_condition(condition, False))

    @patch("django.core.cache.cache.set")
    @patch("django.core.cache.cache.get")
    def test_evaluate_duration_logic(self, mock_get, mock_set):
        condition = AutomationCondition.objects.create(
            team=self.team,
            automation=self.automation,
            device=self.device,
            telemetry_key="pressure",
            operator="lt",
            threshold="10",
            duration_seconds=60,  # Must be low pressure for 1min
        )

        # Step 1: Initial trigger (not yet duration)
        mock_get.return_value = None
        self.assertFalse(evaluate_condition(condition, 5))
        mock_set.assert_called_once()

        # Step 2: Second trigger, but only 30 secs passed
        mock_set.reset_mock()
        mock_get.return_value = {"is_met": True, "met_since": timezone.now().timestamp() - 30}
        self.assertFalse(evaluate_condition(condition, 5))

        # Step 3: Triggered for 65 secs
        mock_get.return_value = {"is_met": True, "met_since": timezone.now().timestamp() - 65}
        self.assertTrue(evaluate_condition(condition, 5))

    def test_evaluate_automations_full_flow(self):
        # Condition: Temp > 100
        AutomationCondition.objects.create(
            team=self.team,
            automation=self.automation,
            device=self.device,
            telemetry_key="temp",
            operator="gt",
            threshold="100",
            duration_seconds=0,
        )
        # Action: Send command
        AutomationAction.objects.create(
            team=self.team,
            automation=self.automation,
            action_type="send_command",
            target_device=self.device,
            command_key="turn_off",
            command_payload={"value": True},
        )

        # Test 1: Condition not met
        evaluate_automations(self.device, {"temp": 90.0, "humidity": 50.0})
        self.assertEqual(AutomationLog.objects.count(), 0)

        # Test 2: Condition met
        evaluate_automations(self.device, {"temp": 110.0, "humidity": 55.0})
        self.assertEqual(AutomationLog.objects.count(), 1)
        self.assertIn("Command proposal created", AutomationLog.objects.get().details)
