from django.test import TestCase
from django.urls import reverse

from apps.devices.models import Device, Site, SiteMembershipAccess
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_MANAGER, ROLE_OWNER, ROLE_VIEWER
from apps.users.models import CustomUser


class SiteBoundaryTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Site boundary", slug="site-boundary")
        self.manager = CustomUser.objects.create_user(
            username="site-manager", email="site-manager@example.com", password="pwd"
        )
        self.owner = CustomUser.objects.create_user(
            username="site-owner", email="site-owner@example.com", password="pwd"
        )
        self.viewer = CustomUser.objects.create_user(
            username="site-viewer", email="site-viewer@example.com", password="pwd"
        )
        membership = Membership.objects.create(team=self.team, user=self.manager, role=ROLE_MANAGER)
        Membership.objects.create(team=self.team, user=self.owner, role=ROLE_OWNER)
        Membership.objects.create(team=self.team, user=self.viewer, role=ROLE_VIEWER)
        self.site_a = Site.objects.create(team=self.team, name="Visible Site A")
        self.site_b = Site.objects.create(team=self.team, name="Private Site B")
        SiteMembershipAccess.objects.create(team=self.team, membership=membership, site=self.site_a)
        self.device_a = Device.objects.create(
            team=self.team, site=self.site_a, name="Visible Pump", device_type="sensor", protocol="modbus_tcp"
        )
        self.device_b = Device.objects.create(
            team=self.team, site=self.site_b, name="Private Pump", device_type="sensor", protocol="modbus_tcp"
        )

    def test_limited_member_sees_only_site_a_and_direct_b_urls_are_denied(self):
        self.client.force_login(self.manager)
        sites = self.client.get(reverse("web_team:devices:site_list", args=[self.team.slug]))
        self.assertEqual(sites.status_code, 200)
        self.assertContains(sites, "Visible Site A")
        self.assertNotContains(sites, "Private Site B")
        devices = self.client.get(reverse("web_team:devices:device_list", args=[self.team.slug]))
        self.assertContains(devices, "Visible Pump")
        self.assertNotContains(devices, "Private Pump")
        self.assertEqual(
            self.client.get(
                reverse("web_team:devices:device_detail", args=[self.team.slug, self.device_b.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("web_team:telemetry:device_telemetry_history_api", args=[self.team.slug, self.device_b.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("web_team:devices:device_edit", args=[self.team.slug, self.device_b.pk]), {"name": "Leaked"}
            ).status_code,
            403,
        )

    def test_owner_retains_both_sites(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("web_team:devices:site_list", args=[self.team.slug]))
        self.assertContains(response, "Visible Site A")
        self.assertContains(response, "Private Site B")

    def test_viewer_navigation_omits_unavailable_destinations(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("web_team:devices:site_list", args=[self.team.slug]))
        for name in ("web_team:onboarding:setup_start", "web_team:dashboard_team:list", "web_team:automations:list"):
            url = reverse(name, args=[self.team.slug])
            self.assertNotIn(f'href="{url}"'.encode(), response.content)
            self.assertEqual(self.client.get(url).status_code, 403)
