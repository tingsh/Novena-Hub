from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.devices.models import Device, Site
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_OWNER
from apps.users.models import CustomUser


class DeviceDirectoryTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Cold Room", slug="cold-room-directory")
        self.site = Site.objects.create(team=self.team, name="Jurong Cold Room", timezone="Asia/Singapore")
        user = CustomUser.objects.create_user(
            username="directory-owner@example.com", email="directory-owner@example.com", password="testpass123"
        )
        Membership.objects.create(team=self.team, user=user, role=ROLE_OWNER)
        self.client.force_login(user)
        self.url = reverse("web_team:devices:device_list", args=[self.team.slug])

    def test_status_links_filter_by_freshness_and_show_site_time(self):
        Device.objects.create(
            team=self.team,
            site=self.site,
            name="Live sensor",
            device_type="temp_sensor",
            protocol="modbus_tcp",
            status="online",
            last_telemetry_at=timezone.now(),
        )
        Device.objects.create(
            team=self.team,
            site=self.site,
            name="Offline sensor",
            device_type="temp_sensor",
            protocol="modbus_tcp",
            status="offline",
            last_telemetry_at=timezone.now() - timedelta(hours=1),
        )

        live = self.client.get(self.url, {"status": "live"})
        self.assertContains(live, "Live sensor")
        self.assertNotContains(live, "Offline sensor")
        self.assertContains(live, "Asia/Singapore time")
        self.assertContains(live, 'aria-current="page"')

        offline = self.client.get(self.url, {"status": "offline"})
        self.assertContains(offline, "Offline sensor")
        self.assertNotContains(offline, "Live sensor")

        alarm = self.client.get(self.url, {"status": "alarm"})
        self.assertContains(alarm, "No devices match this status")
