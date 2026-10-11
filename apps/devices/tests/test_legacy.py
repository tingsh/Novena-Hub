import json
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from apps.alerts.models import AlertRule
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser

from ..models import Device, DeviceCommand, DeviceTemplate, Gateway, GatewayInventory, Site
from ..remote_control import CommandDenied
from ..services import process_command_response, send_device_command


class DeviceInfrastructureTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test-team")
        self.user = CustomUser.objects.create(email="test@example.com", username="testuser")
        self.team.members.add(self.user)
        Membership.objects.filter(team=self.team, user=self.user).update(role="admin")
        self.site = Site.objects.create(team=self.team, name="Test Site")
        self.client = Client()

    def test_direct_to_cloud_connectivity(self):
        device = Device.objects.create(
            team=self.team, site=self.site, name="Cloud Sensor", device_type="sensor", protocol="mqtt"
        )
        self.assertIsNone(device.gateway)
        self.assertEqual(device.site, self.site)

    def test_gateway_discovery_api(self):
        gateway = Gateway.objects.create(
            team=self.team, site=self.site, serial_number="NF-TEST-999", name="Discovery Gateway"
        )
        GatewayInventory.objects.create(
            serial_number=gateway.serial_number,
            status="claimed",
            gateway=gateway,
            claimed_by_team=self.team,
        )
        self.client.force_login(self.user)
        url = reverse("web_team:devices:gateway_discovery_api", args=[self.team.slug])
        payload = {
            "serial_number": "NF-TEST-999",
            "discovered_devices": [{"port": 1, "protocol": "modbus", "slave_id": 5, "signature": "Eastron-SDM630"}],
        }
        response = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(response.status_code, 200)

        gateway.refresh_from_db()
        self.assertIn("Eastron-SDM630", str(gateway.discovery_data))

    def test_template_presets_do_not_create_rules(self):
        template = DeviceTemplate.objects.create(
            name="Smart Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={},
            alert_presets=[{"key": "voltage", "condition": "gt", "threshold": 240.0, "severity": "critical"}],
        )
        device = Device.objects.create(team=self.team, site=self.site, name="Meter 1", template=template)
        rules = AlertRule.objects.filter(device=device)
        self.assertEqual(rules.count(), 0)


class DeviceCommandTest(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create(email="test@example.com", username="testuser")
        self.team = Team.objects.create(name="Test Team", slug="test-team")
        self.site = Site.objects.create(team=self.team, name="Main Plant")
        self.gateway = Gateway.objects.create(team=self.team, site=self.site, name="GW-001", serial_number="SN123")
        self.device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Motor 1",
            device_type="vfd",
            protocol="modbus_tcp",
        )

    @patch("apps.telemetry.mqtt_publisher.publish_rpc_command")
    def test_legacy_write_path_is_governed_and_default_denied(self, mock_publish_rpc):
        with self.assertRaises(CommandDenied):
            send_device_command(self.device, self.user, "toggle_switch", True)
        mock_publish_rpc.assert_not_called()

    def test_process_command_response_success(self):
        command = DeviceCommand.objects.create(
            team=self.team,
            device=self.device,
            command_key="set_speed",
            value=50,
            transaction_id="test-tx-123",
            status="sent",
        )
        response_payload = json.dumps({"device": self.device.name, "id": "test-tx-123", "data": {"success": True}})
        process_command_response(response_payload)
        command.refresh_from_db()
        self.assertEqual(command.status, "executed")


class NotificationTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test-team")

    def test_notification_aggregation(self):
        from apps.alerts.templatetags.notification_tags import get_unread_notifications

        site = Site.objects.create(team=self.team, name="Disco Site")
        Gateway.objects.create(
            team=self.team, site=site, serial_number="NF-DISCO", discovery_data={"devices": [{"port": 1}]}
        )
        data = get_unread_notifications(self.team)
        self.assertEqual(data["unread_count"], 1)
