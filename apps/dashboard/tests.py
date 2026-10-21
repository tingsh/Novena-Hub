from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.dashboard.models import SharedDashboard
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_MANAGER, ROLE_VIEWER
from apps.users.models import CustomUser


class SharedDashboardTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test-team")
        self.user_manager = CustomUser.objects.create_user(username="manager", email="m@ex.com", password="pwd")
        self.user_viewer = CustomUser.objects.create_user(username="viewer", email="v@ex.com", password="pwd")

        Membership.objects.create(team=self.team, user=self.user_manager, role=ROLE_MANAGER)
        Membership.objects.create(team=self.team, user=self.user_viewer, role=ROLE_VIEWER)

        self.link = SharedDashboard.objects.create(team=self.team, name="Reception Screen", is_active=True)
        self.client = Client()

    def test_rbac_manage_links(self):
        # Viewer cannot manage links
        self.client.force_login(self.user_viewer)
        response = self.client.get(reverse("web_team:dashboard_team:list", args=[self.team.slug]))
        self.assertEqual(response.status_code, 403)

        # Manager can manage links
        self.client.force_login(self.user_manager)
        response = self.client.get(reverse("web_team:dashboard_team:list", args=[self.team.slug]))
        self.assertEqual(response.status_code, 200)

    def test_public_dashboard_unauthenticated(self):
        url = reverse("dashboard_public:view", args=[self.link.token])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Team - Reception Screen")
        self.assertContains(response, "Powered by")

        # Check that view count increased
        self.link.refresh_from_db()
        self.assertEqual(self.link.view_count, 1)

    def test_public_dashboard_inactive(self):
        self.link.is_active = False
        self.link.save()

        url = reverse("dashboard_public:view", args=[self.link.token])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_public_dashboard_expired(self):
        self.link.expires_at = timezone.now() - timedelta(days=1)
        self.link.save()

        url = reverse("dashboard_public:view", args=[self.link.token])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_public_dashboard_password_protected(self):
        self.link.set_password("secret123")
        self.link.save()

        url = reverse("dashboard_public:view", args=[self.link.token])

        # GET should prompt for password without incrementing view
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Protected Dashboard")
        self.link.refresh_from_db()
        self.assertEqual(self.link.view_count, 0)

        # POST with wrong password
        response = self.client.post(url, {"password": "wrong"})
        self.assertContains(response, "Incorrect password.")
        self.link.refresh_from_db()
        self.assertEqual(self.link.view_count, 0)

        # POST with correct password
        response = self.client.post(url, {"password": "secret123"})
        self.assertEqual(response.status_code, 200)

        # Follow-up GET should now be allowed and increment view
        response = self.client.get(url)
        self.assertContains(response, "Test Team - Reception Screen")
        self.link.refresh_from_db()
        self.assertEqual(self.link.view_count, 1)

    def test_public_dashboard_kiosk_mode(self):
        url = reverse("dashboard_public:view", args=[self.link.token]) + "?kiosk=1"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # In kiosk mode, the header shouldn't be rendered
        self.assertNotContains(response, "Test Team - Reception Screen")


class AutoDashboardTests(TestCase):
    def setUp(self):
        from apps.devices.models import DeviceTemplate, Site

        self.team = Team.objects.create(name="IoT Team", slug="iot-team")
        self.site = Site.objects.create(team=self.team, name="Main Factory")

        # Create a mock template
        self.template = DeviceTemplate.objects.create(
            name="Test Meter",
            manufacturer="Schneider",
            model_number="M1",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={
                "voltage": {"address": 100, "type": "float32", "unit": "V", "label": "Voltage"},
                "active_power": {"address": 102, "type": "float32", "unit": "kW", "label": "Active Power"},
                "status_bit": {"address": 104, "type": "bool", "label": "Status Coil"},
                "raw_val": {"address": 105, "type": "uint16", "label": "Raw Register"},
            },
        )

    def test_dashboard_and_widgets_generated_on_save(self):
        from apps.dashboard.models import Dashboard, Widget
        from apps.devices.models import Device

        # Provision a device matching the template
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            name="Main Meter 1",
            template=self.template,
            device_type=self.template.device_type,
            protocol=self.template.protocol,
        )

        # Check that dashboard was auto-generated
        dashboard = Dashboard.objects.filter(device=device, is_default=True).first()
        self.assertIsNotNone(dashboard)
        self.assertEqual(dashboard.name, "Main Meter 1 Overview")

        # Check that correct widgets were generated
        widgets = Widget.objects.filter(dashboard=dashboard)
        self.assertEqual(widgets.count(), 4)

        # Verify widget types
        voltage_widget = widgets.get(telemetry_key="voltage")
        self.assertEqual(voltage_widget.widget_type, "gauge")
        self.assertEqual(voltage_widget.unit, "V")

        power_widget = widgets.get(telemetry_key="active_power")
        self.assertEqual(power_widget.widget_type, "timeseries")
        self.assertEqual(power_widget.unit, "kW")

        status_widget = widgets.get(telemetry_key="status_bit")
        self.assertEqual(status_widget.widget_type, "indicator")

        raw_widget = widgets.get(telemetry_key="raw_val")
        self.assertEqual(raw_widget.widget_type, "value")

        # Idempotence: save device again, widget count should not double
        device.name = "Main Meter 1 Rename"
        device.save()
        self.assertEqual(Widget.objects.filter(dashboard=dashboard).count(), 4)


class AdaptiveOperationsDashboardTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        from apps.devices.models import Site

        cache.clear()
        self.team = Team.objects.create(name="Ops Team", slug="ops-team")
        self.site = Site.objects.create(team=self.team, name="Factory")

    def _template(self, name, device_type, register_map, category="energy"):
        from apps.devices.models import DeviceTemplate

        return DeviceTemplate.objects.create(
            name=name,
            device_type=device_type,
            protocol="modbus_tcp",
            category=category,
            register_map=register_map,
        )

    def _device(self, name, template, device_type=None, gateway=None):
        from apps.devices.models import Device

        return Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=gateway,
            name=name,
            template=template,
            device_type=device_type or template.device_type,
            protocol=template.protocol,
        )

    def test_energy_device_produces_energy_widgets_and_trend(self):
        from django.utils import timezone

        from apps.dashboard.services import build_team_operations_dashboard
        from apps.telemetry.models import TelemetryData

        template = self._template(
            "Power Meter",
            "power_meter",
            {"active_power": {"label": "Demand", "unit": "W", "dashboard_role": "trend"}},
        )
        device = self._device("Main Meter", template)
        TelemetryData.objects.create(device=device, timestamp=timezone.now(), key="active_power", value_numeric=42.0)

        dashboard = build_team_operations_dashboard(self.team)

        self.assertTrue(dashboard["has_energy_widgets"])
        self.assertEqual(dashboard["metric_groups"][0]["label"], "Energy")
        self.assertEqual(dashboard["operations_trend"]["key"], "active_power")
        self.assertEqual(dashboard["top_devices"][0]["latest_reading"]["display_value"], "42.0")

    def test_temperature_trend_averages_one_device_and_keeps_missing_hours_empty(self):
        from zoneinfo import ZoneInfo

        from django.core.cache import cache

        from apps.dashboard.services import build_team_operations_dashboard
        from apps.telemetry.models import TelemetryData

        self.site.timezone = "Asia/Singapore"
        self.site.save(update_fields=["timezone"])
        template = self._template(
            "Cold Room",
            "temp_sensor",
            {"temperature": {"label": "Temperature", "unit": "degC", "dashboard_role": "trend"}},
            category="cold_chain",
        )
        device = self._device("Room A", template)
        another_device = self._device("Room B", template)
        now = timezone.now()
        TelemetryData.objects.create(
            device=device, timestamp=now - timedelta(minutes=2), key="temperature", value_numeric=2
        )
        TelemetryData.objects.create(
            device=device, timestamp=now - timedelta(minutes=1), key="temperature", value_numeric=4
        )
        TelemetryData.objects.create(
            device=another_device, timestamp=now - timedelta(minutes=1), key="temperature", value_numeric=80
        )
        cache.clear()

        dashboard = build_team_operations_dashboard(self.team)
        trend = dashboard["operations_trend"]

        self.assertEqual(trend["values"][-1], 3)
        self.assertIsNone(trend["values"][-2])
        self.assertEqual(trend["unit"], "°C")
        self.assertEqual(trend["labels"][-1], timezone.localtime(now, ZoneInfo("Asia/Singapore")).strftime("%H:%M"))
        self.assertIn("Room A · hourly average", trend["subtitle"])
        self.assertEqual(dashboard["top_devices"][0]["latest_reading"]["unit"], "°C")

    def test_cumulative_energy_trend_uses_last_hourly_reading(self):
        from django.core.cache import cache

        from apps.dashboard.services import build_team_operations_dashboard
        from apps.telemetry.models import TelemetryData

        template = self._template("Energy Meter", "power_meter", {"energy": {"label": "Energy", "unit": "kWh"}})
        device = self._device("Main Meter", template)
        now = timezone.now()
        TelemetryData.objects.create(
            device=device, timestamp=now - timedelta(minutes=2), key="energy", value_numeric=100
        )
        TelemetryData.objects.create(
            device=device, timestamp=now - timedelta(minutes=1), key="energy", value_numeric=101
        )
        cache.clear()

        trend = build_team_operations_dashboard(self.team)["operations_trend"]

        self.assertEqual(trend["values"][-1], 101)
        self.assertIn("hourly last reading", trend["subtitle"])

    def test_delayed_device_appears_in_needs_attention(self):
        from apps.dashboard.services import build_team_operations_dashboard

        template = self._template("Cold Room", "temp_sensor", {"temperature": {"unit": "degC"}}, "cold_chain")
        device = self._device("Delayed Room", template)
        device.last_telemetry_at = timezone.now() - timedelta(seconds=20)
        device.save(update_fields=["last_telemetry_at"])

        dashboard = build_team_operations_dashboard(self.team)

        self.assertEqual(dashboard["devices_attention"], 1)
        self.assertEqual(dashboard["attention_items"][0]["title"], "Delayed Room")
        self.assertIn("Delayed - last sample", dashboard["attention_items"][0]["message"])

    def test_unconfigured_alert_monitoring_is_not_presented_as_all_clear(self):
        from apps.dashboard.services import build_team_operations_dashboard

        template = self._template("Cold Room", "temp_sensor", {"temperature": {"unit": "degC"}}, "cold_chain")
        self._device("Room A", template)

        dashboard = build_team_operations_dashboard(self.team)

        self.assertEqual(dashboard["active_rule_count"], 0)
        self.assertIn("Alert monitoring not set up", [item["title"] for item in dashboard["attention_items"]])

    def test_vfd_with_default_energy_category_is_classified_as_motor(self):
        from apps.dashboard.services import build_team_operations_dashboard

        template = self._template(
            "VFD",
            "vfd",
            {"output_frequency": {"label": "Output Frequency", "unit": "Hz"}, "current": {"unit": "A"}},
        )
        self._device("Pump Drive", template)

        dashboard = build_team_operations_dashboard(self.team)

        self.assertEqual(dashboard["metric_groups"][0]["label"], "Motor / VFD")
        self.assertFalse(dashboard["has_energy_widgets"])

    def test_pump_keys_classify_unknown_device_as_pump(self):
        from apps.dashboard.services import build_team_operations_dashboard

        template = self._template(
            "Pump Controller",
            "other",
            {"pressure": {"label": "Pressure", "unit": "bar"}, "flow_rate": {"label": "Flow", "unit": "m3/h"}},
            category="factory",
        )
        self._device("Booster Pump", template, device_type="other")

        dashboard = build_team_operations_dashboard(self.team)

        self.assertEqual(dashboard["metric_groups"][0]["label"], "Pump")

    def test_unknown_device_falls_back_to_latest_telemetry(self):
        from django.utils import timezone

        from apps.dashboard.services import build_team_operations_dashboard
        from apps.devices.models import Device
        from apps.telemetry.models import TelemetryData

        device = Device.objects.create(
            team=self.team,
            site=self.site,
            name="Unknown Sensor",
            device_type="other",
            protocol="mqtt",
        )
        TelemetryData.objects.create(device=device, timestamp=timezone.now(), key="custom_metric", value_numeric=7.5)

        dashboard = build_team_operations_dashboard(self.team)

        self.assertEqual(dashboard["metric_groups"][0]["label"], "General")
        self.assertEqual(dashboard["top_devices"][0]["latest_reading"]["label"], "Custom Metric")

    def test_mixed_fleet_prioritizes_gateway_online_device_offline_attention(self):
        from django.utils import timezone

        from apps.dashboard.services import build_team_operations_dashboard
        from apps.devices.models import Device, Gateway

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Online Gateway",
            serial_number="GW-ATTN-001",
            access_token="attn-token",
            status="online",
            last_seen=timezone.now(),
        )
        template = self._template("Meter", "power_meter", {"active_power": {"unit": "W"}})
        Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=gateway,
            template=template,
            name="Offline Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            status="offline",
            last_telemetry_at=timezone.now() - timezone.timedelta(minutes=5),
        )

        dashboard = build_team_operations_dashboard(self.team)

        self.assertEqual(dashboard["attention_items"][0]["message"], "Gateway online - device offline")

    def test_template_priority_and_dashboard_role_influence_widgets(self):
        from apps.dashboard.models import Widget

        template = self._template(
            "Hinted Template",
            "power_meter",
            {
                "raw_register": {"label": "Raw", "priority": 50},
                "important_temp": {"label": "Important Temp", "unit": "°C", "priority": 1, "dashboard_role": "trend"},
                "write_only": {"label": "Write Only", "writable": True},
            },
        )
        device = self._device("Hinted Device", template)
        widgets = list(Widget.objects.filter(device=device).order_by("row", "col"))

        self.assertEqual(widgets[0].telemetry_key, "important_temp")
        self.assertEqual(widgets[0].widget_type, "timeseries")
        self.assertFalse(Widget.objects.filter(device=device, telemetry_key="write_only").exists())

    def test_site_solution_profile_prioritizes_dashboard_widgets(self):
        from apps.dashboard.models import Widget

        self.site.solution_profile = "facilities_hvac"
        self.site.save(update_fields=["solution_profile"])
        template = self._template(
            "Facilities Meter",
            "chiller",
            {
                "active_power": {"label": "Power", "unit": "W", "priority": 1},
                "temperature": {"label": "Supply Temperature", "unit": "degC", "priority": 50},
                "run_hours": {"label": "Run Hours", "unit": "h", "priority": 50},
            },
            category="factory",
        )
        device = self._device("Level 3 HVAC", template, device_type="chiller")

        widgets = list(Widget.objects.filter(device=device).order_by("row", "col"))

        self.assertEqual(widgets[0].telemetry_key, "temperature")
        self.assertEqual(widgets[1].telemetry_key, "active_power")
