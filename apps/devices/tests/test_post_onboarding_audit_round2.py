from datetime import timedelta
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.automations.models import Automation
from apps.dashboard.models import SharedDashboard
from apps.devices.models import Device, Gateway, Site
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_OWNER
from apps.telemetry.models import TelemetryData
from apps.users.models import CustomUser


class PostOnboardingCustomerAuditTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Cold Room", slug="audit-round2-cold-room")
        self.site = Site.objects.create(
            team=self.team, name="Jurong Cold Room", solution_profile="cold_chain", timezone="Asia/Singapore"
        )
        self.user = CustomUser.objects.create_user(
            username="audit-owner", email="audit-owner@example.com", password="test"
        )
        Membership.objects.create(team=self.team, user=self.user, role=ROLE_OWNER)
        self.client.force_login(self.user)
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            serial_number="AUDIT-CM4-2",
            name="Cold Room Gateway",
            lifecycle_status="online",
            status="online",
            last_seen=timezone.now(),
        )
        self.device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Cold Room Sensor",
            device_type="temp_sensor",
            protocol="modbus_tcp",
            port="10.0.0.20:502",
            last_telemetry_at=timezone.now() - timedelta(minutes=4),
        )

    def test_site_and_gateway_show_configured_equipment_without_a_fresh_scan(self):
        self.gateway.discovery_data = {
            "devices": [{"interface": "10.0.0.20:502", "signature": "Modbus device"}],
            "interfaces": [{"name": "uart0", "label": "Built-in UART", "protocols": ["modbus_rtu"]}],
        }
        self.gateway.save(update_fields=["discovery_data"])
        with patch("apps.telemetry.services.get_site_summary_stats", return_value={"current_power": 0, "daily_kwh": 0}):
            response = self.client.get(reverse("web_team:devices:site_detail", args=[self.team.slug, self.site.id]))
        self.assertContains(response, "Cold Room Sensor")
        self.assertContains(response, "Configured equipment is shown below")
        self.assertNotContains(response, "No devices discovered yet")
        self.assertNotContains(response, "Hardware Detection Active")
        self.assertNotContains(response, "Built-in UART")
        self.assertNotContains(response, "Current Demand")

        directory = self.client.get(reverse("web_team:devices:gateway_list", args=[self.team.slug]))
        self.assertContains(directory, "1 configured equipment")
        detail = self.client.get(reverse("web_team:devices:gateway_detail", args=[self.team.slug, self.gateway.id]))
        self.assertContains(detail, "Check each equipment status below")
        devices = self.client.get(reverse("web_team:devices:device_list", args=[self.team.slug]))
        self.assertContains(devices, "Set up equipment")
        self.assertContains(devices, reverse("web_team:onboarding:setup_start", args=[self.team.slug]))

    def test_dashboard_site_card_does_not_claim_healthy_without_monitoring(self):
        self.device.last_telemetry_at = timezone.now()
        self.device.save(update_fields=["last_telemetry_at"])
        response = self.client.get(reverse("web_team:home", args=[self.team.slug]))
        card = response.context["site_cards"][0]
        self.assertFalse(card["monitoring_active"])
        panel = render_to_string(
            "web/command_center/panels/sites_actions.html",
            {"team": self.team, "site_cards": [card], "can_manage_alerts": False},
        )
        self.assertIn("Monitoring not set up", panel)
        self.assertIn("1 device · 1 gateway", panel)
        self.assertNotIn(">Healthy<", panel)

    def test_value_report_marks_old_readings_and_formats_customer_values(self):
        old = timezone.now() - timedelta(minutes=4)
        TelemetryData.objects.create(device=self.device, timestamp=old, key="temperature", value_numeric=2.71)
        TelemetryData.objects.create(device=self.device, timestamp=old, key="door_open", value_bool=False)

        response = self.client.get(
            reverse("web_team:telemetry:site_profile_report", args=[self.team.slug, self.site.id])
        )
        self.assertContains(response, "2.71")
        self.assertContains(response, "Closed")
        self.assertContains(response, "Offline")
        self.assertNotContains(response, "False")

        csv_response = self.client.get(
            reverse("web_team:telemetry:export_site_profile_report_csv", args=[self.team.slug, self.site.id])
        )
        self.assertIn("Reading Status", csv_response.content.decode())
        self.assertIn("Closed", csv_response.content.decode())

    def test_public_link_starts_inactive_and_public_summary_never_claims_healthy_without_monitoring(self):
        form = self.client.get(reverse("web_team:dashboard_team:create", args=[self.team.slug]))
        self.assertContains(form, "New links stay inactive")
        self.assertFalse(form.context["form"]["is_active"].value())

        created = self.client.post(
            reverse("web_team:dashboard_team:create", args=[self.team.slug]), {"name": "Test view"}
        )
        self.assertEqual(created.status_code, 302)
        self.assertFalse(SharedDashboard.objects.get(name="Test view").is_active)

        link = SharedDashboard.objects.create(team=self.team, name="Active test view", is_active=True)
        self.client.logout()
        with patch(
            "apps.telemetry.services.get_site_summary_stats", return_value={"total_devices": 1, "active_alerts": 0}
        ):
            public = self.client.get(reverse("dashboard_public:view", args=[link.token]))
        self.assertContains(public, "devices need attention")
        self.assertNotContains(public, "Healthy")
        self.assertNotContains(public, "Est. Energy (24h)")

    def test_invalid_automation_action_does_not_create_an_automation(self):
        url = reverse("web_team:automations:create", args=[self.team.slug])
        form = self.client.get(url)
        self.assertFalse(form.context["form"]["is_active"].value())
        response = self.client.post(
            url,
            {
                "name": "Invalid test rule",
                "trigger_logic": "and",
                "cooldown_minutes": "5",
                "conditions-TOTAL_FORMS": "1",
                "conditions-INITIAL_FORMS": "0",
                "conditions-MIN_NUM_FORMS": "0",
                "conditions-MAX_NUM_FORMS": "1000",
                "actions-TOTAL_FORMS": "1",
                "actions-INITIAL_FORMS": "0",
                "actions-MIN_NUM_FORMS": "0",
                "actions-MAX_NUM_FORMS": "1000",
                "actions-0-action_type": "not_a_real_action",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Automation.objects.filter(name="Invalid test rule").exists())
