from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.alerts.models import Alert, AlertRule
from apps.devices.models import Device, Gateway, Site, SiteMembershipAccess
from apps.maintenance.models import MaintenanceTicket
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_MANAGER, ROLE_OWNER, ROLE_VIEWER
from apps.telemetry.models import TelemetryData
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
        home = self.client.get(reverse("web_team:home", args=[self.team.slug]))
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "Visible Site A")
        self.assertNotContains(home, "Private Site B")
        self.assertNotContains(home, 'href="' + reverse("web_team:onboarding:setup_start", args=[self.team.slug]) + '"')
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

    def test_limited_member_http_api_and_aggregate_scope(self):
        for site, device, label in (
            (self.site_a, self.device_a, "Visible"),
            (self.site_b, self.device_b, "Private"),
        ):
            rule = AlertRule.objects.create(
                team=self.team,
                site=site,
                device=device,
                name=f"{label} warning",
                telemetry_key="temperature",
                condition="gt",
                threshold=10,
                notify_email=False,
            )
            Alert.objects.create(team=self.team, rule=rule, device=device, trigger_value=12, status="active")
            MaintenanceTicket.objects.create(
                team=self.team,
                device=device,
                title=f"{label} work",
                send_email_notification=False,
            )
            TelemetryData.objects.create(device=device, key="temperature", timestamp=timezone.now(), value_numeric=12)

        self.client.force_login(self.manager)
        for route in (
            "device_metrics_api",
            "device_telemetry_history_api",
            "device_telemetry_samples_api",
            "export_telemetry_csv",
            "telemetry_analyzer",
        ):
            url_a = reverse(f"web_team:telemetry:{route}", args=[self.team.slug, self.device_a.pk])
            url_b = reverse(f"web_team:telemetry:{route}", args=[self.team.slug, self.device_b.pk])
            self.assertEqual(self.client.get(url_a).status_code, 200, route)
            self.assertEqual(self.client.get(url_b).status_code, 404, route)

        for route in (
            "web_team:home",
            "web_team:alerts:alert_list",
            "web_team:alerts:rule_list",
            "web_team:maintenance:ticket_list",
        ):
            response = self.client.get(reverse(route, args=[self.team.slug]))
            self.assertEqual(response.status_code, 200, route)
            self.assertContains(response, "Visible")
            self.assertNotContains(response, "Private")

        ticket_b = MaintenanceTicket.objects.get(device=self.device_b)
        self.assertEqual(
            self.client.get(
                reverse("web_team:maintenance:ticket_detail", args=[self.team.slug, ticket_b.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("web_team:devices:device_edit", args=[self.team.slug, self.device_a.pk]), {"name": "Changed"}
            ).status_code,
            403,
        )

    def test_viewer_navigation_omits_unavailable_destinations(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("web_team:devices:site_list", args=[self.team.slug]))
        self.assertContains(response, "document.getElementById('contact-support-modal').showModal()", count=2)
        for name in ("web_team:onboarding:setup_start", "web_team:dashboard_team:list", "web_team:automations:list"):
            url = reverse(name, args=[self.team.slug])
            self.assertNotIn(f'href="{url}"'.encode(), response.content)
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_site_and_gateway_back_links_have_team_destinations(self):
        self.client.force_login(self.owner)
        home = reverse("web_team:home", args=[self.team.slug])
        detail = self.client.get(reverse("web_team:devices:site_detail", args=[self.team.slug, self.site_a.pk]))
        self.assertContains(detail, f'href="{home}" aria-label="Dashboard"')

        site_create = self.client.get(reverse("web_team:devices:site_create", args=[self.team.slug]))
        site_edit = self.client.get(reverse("web_team:devices:site_edit", args=[self.team.slug, self.site_a.pk]))
        self.assertContains(site_create, f'href="{reverse("web_team:devices:site_list", args=[self.team.slug])}"')
        self.assertContains(
            site_edit, f'href="{reverse("web_team:devices:site_detail", args=[self.team.slug, self.site_a.pk])}"'
        )

        gateway = Gateway.objects.create(
            team=self.team, site=self.site_a, name="Site A Gateway", serial_number="RC-GATEWAY-A", access_token="token"
        )
        gateway_create = self.client.get(reverse("web_team:devices:gateway_create", args=[self.team.slug]))
        gateway_edit = self.client.get(reverse("web_team:devices:gateway_edit", args=[self.team.slug, gateway.pk]))
        self.assertContains(gateway_create, f'href="{reverse("web_team:devices:gateway_list", args=[self.team.slug])}"')
        self.assertContains(
            gateway_edit, f'href="{reverse("web_team:devices:gateway_detail", args=[self.team.slug, gateway.pk])}"'
        )
