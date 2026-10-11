from django.test import Client, TestCase
from django.urls import reverse

from apps.alerts.models import AlertRule
from apps.devices.models import Device, DeviceTemplate, Gateway, GatewayInventory, Site
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser


class DeviceInfrastructureTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test-team")
        self.user = CustomUser.objects.create(email="test@example.com", username="testuser")
        self.team.members.add(self.user)
        Membership.objects.filter(team=self.team, user=self.user).update(role="admin")
        self.site = Site.objects.create(team=self.team, name="Test Site")
        self.client = Client()

    def test_direct_to_cloud_connectivity(self):
        """Phase 1: Verify device creation without gateway."""
        device = Device.objects.create(
            team=self.team, site=self.site, name="Cloud Sensor", device_type="sensor", protocol="mqtt"
        )
        self.assertIsNone(device.gateway)
        self.assertEqual(device.site, self.site)

    def test_gateway_discovery_api(self):
        """Phase 3: Verify Discovery API updates gateway data."""
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
        """Equipment templates must not silently enable alert rules."""
        template = DeviceTemplate.objects.create(
            name="Smart Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={},
            alert_presets=[{"key": "voltage", "condition": "gt", "threshold": 240.0, "severity": "critical"}],
        )

        device = Device.objects.create(team=self.team, site=self.site, name="Meter 1", template=template)

        self.assertFalse(AlertRule.objects.filter(device=device).exists())


class NotificationTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test-team")

    def test_notification_aggregation(self):
        """Verify the template tag aggregates data correctly."""
        from apps.alerts.templatetags.notification_tags import get_unread_notifications

        site = Site.objects.create(team=self.team, name="Disco Site")
        # Mock a discovery
        Gateway.objects.create(
            team=self.team, site=site, serial_number="NF-DISCO", discovery_data={"devices": [{"port": 1}]}
        )

        data = get_unread_notifications(self.team)
        self.assertEqual(data["unread_count"], 1)
        self.assertEqual(len(data["discoveries"]), 1)


class DeviceFreshnessTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Fresh Team", slug="fresh-team")
        self.site = Site.objects.create(team=self.team, name="Fresh Site")
        self.template = DeviceTemplate.objects.create(
            name="Five Second Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={},
            default_polling_interval=5,
        )
        self.device = Device.objects.create(
            team=self.team,
            site=self.site,
            name="Fresh Meter",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_tcp",
            status="online",
        )

    def test_fresh_device_resolves_to_live(self):
        from django.utils import timezone

        self.device.last_telemetry_at = timezone.now() - timezone.timedelta(seconds=4)
        state = self.device.freshness

        self.assertEqual(state.status, "live")
        self.assertIn("Live", state.display)

    def test_late_device_resolves_to_delayed(self):
        from django.utils import timezone

        self.device.last_telemetry_at = timezone.now() - timezone.timedelta(seconds=21)
        state = self.device.freshness

        self.assertEqual(state.status, "delayed")
        self.assertIn("Delayed", state.display)

    def test_stale_device_resolves_to_offline(self):
        from django.utils import timezone

        self.device.last_telemetry_at = timezone.now() - timezone.timedelta(seconds=31)
        state = self.device.freshness

        self.assertEqual(state.status, "offline")
        self.assertIn("Offline", state.display)

    def test_alarm_status_has_priority_over_freshness(self):
        from django.utils import timezone

        self.device.status = "alarm"
        self.device.last_telemetry_at = timezone.now() - timezone.timedelta(seconds=4)
        state = self.device.freshness

        self.assertEqual(state.status, "alarm")
        self.assertIn("Alarm", state.display)

    def test_slow_polling_interval_avoids_false_offline(self):
        from django.utils import timezone

        self.template.default_polling_interval = 60
        self.template.save(update_fields=["default_polling_interval"])
        self.device.last_telemetry_at = timezone.now() - timezone.timedelta(seconds=120)
        state = self.device.freshness

        self.assertEqual(state.status, "delayed")

    def test_check_device_heartbeats_marks_stale_online_device_offline(self):
        from django.utils import timezone

        from apps.devices.tasks import check_device_heartbeats

        self.device.last_telemetry_at = timezone.now() - timezone.timedelta(seconds=45)
        self.device.save(update_fields=["last_telemetry_at"])

        self.assertEqual(check_device_heartbeats(), 1)
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, "offline")

    def test_check_device_heartbeats_does_not_clear_alarm(self):
        from django.utils import timezone

        from apps.devices.tasks import check_device_heartbeats

        self.device.status = "alarm"
        self.device.last_telemetry_at = timezone.now() - timezone.timedelta(seconds=45)
        self.device.save(update_fields=["status", "last_telemetry_at"])

        self.assertEqual(check_device_heartbeats(), 0)
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, "alarm")


class GatewayFreshnessTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Gateway Fresh Team", slug="gateway-fresh-team")
        self.site = Site.objects.create(team=self.team, name="Gateway Site")

    def test_gateway_heartbeat_timeout_marks_gateway_offline(self):
        from django.test import override_settings
        from django.utils import timezone

        from apps.devices.tasks import check_gateway_heartbeats

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Stale Gateway",
            serial_number="GW-FRESH-001",
            access_token="gw-fresh-token",
            status="online",
            last_seen=timezone.now() - timezone.timedelta(seconds=121),
        )

        with override_settings(GATEWAY_OFFLINE_SECONDS=120):
            self.assertEqual(check_gateway_heartbeats(), 1)

        gateway.refresh_from_db()
        self.assertEqual(gateway.status, "offline")
        self.assertIn("Gateway offline", gateway.freshness.display)

    def test_gateway_online_device_offline_context_copy(self):
        from django.utils import timezone

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Reachable Gateway",
            serial_number="GW-FRESH-002",
            access_token="gw-fresh-token-002",
            status="online",
            last_seen=timezone.now() - timezone.timedelta(seconds=18),
        )
        template = DeviceTemplate.objects.create(
            name="Fast Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={},
            default_polling_interval=5,
        )
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=gateway,
            name="Offline Field Meter",
            template=template,
            device_type="power_meter",
            protocol="modbus_tcp",
            status="offline",
            last_telemetry_at=timezone.now() - timezone.timedelta(minutes=5),
        )

        self.assertEqual(device.gateway_context_display, "Gateway online - device offline")

    def test_gateway_recent_heartbeat_resolves_live(self):
        from django.test import override_settings
        from django.utils import timezone

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Fresh Gateway",
            serial_number="GW-FRESH-003",
            access_token="gw-fresh-token-003",
            status="online",
            last_seen=timezone.now() - timezone.timedelta(seconds=30),
        )

        with override_settings(GATEWAY_OFFLINE_SECONDS=120):
            self.assertEqual(gateway.freshness.status, "live")
            self.assertIn("Gateway online", gateway.freshness.display)

    def test_gateway_stale_heartbeat_resolves_offline_without_celery(self):
        from django.test import override_settings
        from django.utils import timezone

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Computed Stale Gateway",
            serial_number="GW-FRESH-004",
            access_token="gw-fresh-token-004",
            status="online",
            last_seen=timezone.now() - timezone.timedelta(seconds=121),
        )

        with override_settings(GATEWAY_OFFLINE_SECONDS=120):
            self.assertEqual(gateway.freshness.status, "offline")
            self.assertIn("Gateway offline", gateway.freshness.display)

    def test_gateway_context_uses_gateway_offline_when_gateway_is_stale(self):
        from django.test import override_settings
        from django.utils import timezone

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Stale Context Gateway",
            serial_number="GW-FRESH-005",
            access_token="gw-fresh-token-005",
            status="online",
            last_seen=timezone.now() - timezone.timedelta(seconds=121),
        )
        template = DeviceTemplate.objects.create(
            name="Context Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={},
            default_polling_interval=5,
        )
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=gateway,
            name="Offline Context Meter",
            template=template,
            device_type="power_meter",
            protocol="modbus_tcp",
            status="offline",
            last_telemetry_at=timezone.now() - timezone.timedelta(minutes=5),
        )

        with override_settings(GATEWAY_OFFLINE_SECONDS=120):
            self.assertIn("Gateway offline", device.gateway_context_display)

    def test_device_detail_context_uses_plan_safe_fallback_polling_interval(self):
        from django.test import RequestFactory

        from apps.devices.views import DeviceDetailView

        device = Device.objects.create(
            team=self.team,
            site=self.site,
            name="Fallback Interval Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            status="online",
        )
        request = RequestFactory().get("/devices/1/")
        request.team = self.team
        view = DeviceDetailView()
        view.request = request
        view.object = device

        context = view.get_context_data(object=device)

        self.assertEqual(context["telemetry_fallback_interval_ms"], 10000)


class SiteDeleteFlowTest(TestCase):
    def setUp(self):
        from apps.teams.models import Membership
        from apps.teams.roles import ROLE_OWNER

        self.team = Team.objects.create(name="Delete Team", slug="delete-team")
        self.user = CustomUser.objects.create_user(
            email="owner-delete@example.com",
            username="owner-delete@example.com",
            password="testpass123",
        )
        Membership.objects.create(user=self.user, team=self.team, role=ROLE_OWNER)
        self.site = Site.objects.create(team=self.team, name="Wrongly Typed Site")
        self.device = Device.objects.create(
            team=self.team,
            site=self.site,
            name="Temporary Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
        )
        self.client.force_login(self.user)

    def test_site_delete_confirmation_page_renders(self):
        response = self.client.get(reverse("web_team:devices:site_delete", args=[self.team.slug, self.site.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete Site")
        self.assertContains(response, "Type Wrongly Typed Site to confirm deletion")
        self.assertContains(response, "Deleting this site will also delete")

    def test_site_delete_rejects_wrong_confirmation_name(self):
        response = self.client.post(
            reverse("web_team:devices:site_delete", args=[self.team.slug, self.site.pk]),
            data={"confirmation_name": "Wrong name"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Type the site name exactly to confirm deletion.")
        self.assertTrue(Site.objects.filter(pk=self.site.pk).exists())
        self.assertTrue(Device.objects.filter(pk=self.device.pk).exists())

    def test_site_delete_with_exact_confirmation_deletes_site_and_devices(self):
        response = self.client.post(
            reverse("web_team:devices:site_delete", args=[self.team.slug, self.site.pk]),
            data={"confirmation_name": "Wrongly Typed Site"},
        )

        self.assertRedirects(response, reverse("web_team:devices:site_list", args=[self.team.slug]))
        self.assertFalse(Site.objects.filter(pk=self.site.pk).exists())
        self.assertFalse(Device.objects.filter(pk=self.device.pk).exists())

    def test_site_delete_refuses_to_bypass_secure_gateway_release(self):
        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Managed Gateway",
            serial_number="NF-SITE-DELETE-001",
            access_token="site-delete-token",
            mqtt_username="NF-SITE-DELETE-001",
            mqtt_password="credential-hash",
        )
        inventory = GatewayInventory.objects.create(
            serial_number=gateway.serial_number,
            status="claimed",
            gateway=gateway,
            claimed_by_team=self.team,
        )

        response = self.client.post(
            reverse("web_team:devices:site_delete", args=[self.team.slug, self.site.pk]),
            data={"confirmation_name": self.site.name},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "securely release them first")
        self.assertTrue(Site.objects.filter(pk=self.site.pk).exists())
        self.assertTrue(Gateway.objects.filter(pk=gateway.pk).exists())
        inventory.refresh_from_db()
        self.assertEqual(inventory.status, "claimed")
        self.assertEqual(inventory.gateway, gateway)
