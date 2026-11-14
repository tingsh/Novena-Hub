import json

from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from waffle.testutils import override_flag

from apps.dashboard.command_center import (
    LayoutValidationError,
    available_panel_ids,
    normalize_layout,
    novena_default_layout,
    resolve_layout,
    validate_layout_payload,
)
from apps.dashboard.models import CommandCenterLayout
from apps.devices.models import Device, Site
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_OWNER, ROLE_VIEWER
from apps.users.models import CustomUser


class CommandCenterLayoutTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Cold Chain", slug="cold-chain")
        self.owner = CustomUser.objects.create_user(
            username="owner@example.com",
            email="owner@example.com",
            password="testpass123",
        )
        Membership.objects.create(team=self.team, user=self.owner, role=ROLE_OWNER)

    def test_resolver_prefers_personal_then_team_default(self):
        team_layout = novena_default_layout()
        team_layout["panels"][2]["hidden"] = True
        CommandCenterLayout.objects.create(
            team=self.team,
            scope=CommandCenterLayout.Scope.TEAM_DEFAULT,
            layout=team_layout,
        )

        resolved = resolve_layout(self.team, self.owner)
        self.assertEqual(resolved["source"], CommandCenterLayout.Scope.TEAM_DEFAULT)
        attention_panel = next(panel for panel in resolved["layout"]["panels"] if panel["id"] == "needs_attention")
        self.assertTrue(attention_panel["hidden"])

        personal_layout = novena_default_layout()
        personal_layout["panels"][3]["hidden"] = True
        CommandCenterLayout.objects.create(
            team=self.team,
            user=self.owner,
            scope=CommandCenterLayout.Scope.PERSONAL,
            layout=personal_layout,
        )
        resolved = resolve_layout(self.team, self.owner)
        self.assertEqual(resolved["source"], CommandCenterLayout.Scope.PERSONAL)
        self.assertTrue(next(panel for panel in resolved["layout"]["panels"] if panel["id"] == "asset_mix")["hidden"])

    def test_normalize_layout_adds_missing_registered_panels_and_drops_retired_ids(self):
        stored = {
            "schema_version": 1,
            "panels": [
                {"id": "needs_attention", "x": 0, "y": 0, "w": 4, "h": 4, "hidden": False, "mobile_order": 0},
                {"id": "retired", "x": 0, "y": 9, "w": 4, "h": 4, "hidden": False, "mobile_order": 1},
            ],
        }
        normalized = normalize_layout(stored)
        self.assertEqual(len(normalized["panels"]), 6)
        self.assertNotIn("retired", {panel["id"] for panel in normalized["panels"]})

    def test_validation_rejects_overlap_and_preserves_unavailable_panel(self):
        base = novena_default_layout()
        submitted = [dict(panel) for panel in base["panels"] if panel["id"] != "business_impact"]
        submitted[1].update(submitted[0])
        submitted[1]["id"] = "needs_attention"
        payload = {"schema_version": 1, "panels": submitted}

        with self.assertRaises(LayoutValidationError):
            validate_layout_payload(
                payload,
                available_ids=available_panel_ids(include_impact=False),
                base_layout=base,
            )

        valid = [dict(panel) for panel in base["panels"] if panel["id"] != "business_impact"]
        normalized = validate_layout_payload(
            {"schema_version": 1, "panels": valid},
            available_ids=available_panel_ids(include_impact=False),
            base_layout=base,
        )
        self.assertIn("business_impact", {panel["id"] for panel in normalized["panels"]})

    def test_constraints_allow_only_one_team_default(self):
        CommandCenterLayout.objects.create(
            team=self.team,
            scope=CommandCenterLayout.Scope.TEAM_DEFAULT,
            layout=novena_default_layout(),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommandCenterLayout.objects.create(
                team=self.team,
                scope=CommandCenterLayout.Scope.TEAM_DEFAULT,
                layout=novena_default_layout(),
            )


class CommandCenterEndpointTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Cold Chain", slug="cold-chain")
        self.other_team = Team.objects.create(name="Factory", slug="factory")
        self.owner = CustomUser.objects.create_user(
            username="owner@example.com",
            email="owner@example.com",
            password="testpass123",
        )
        self.viewer = CustomUser.objects.create_user(
            username="viewer@example.com",
            email="viewer@example.com",
            password="testpass123",
        )
        Membership.objects.create(team=self.team, user=self.owner, role=ROLE_OWNER)
        Membership.objects.create(team=self.team, user=self.viewer, role=ROLE_VIEWER)
        self.client.force_login(self.owner)

    def url(self, name, team=None):
        return reverse(f"web_team:command_center:{name}", args=[(team or self.team).slug])

    def payload(self, *, revision=0):
        panels = [panel for panel in novena_default_layout()["panels"] if panel["id"] != "business_impact"]
        return {"schema_version": 1, "base_revision": revision, "panels": panels}

    @override_flag("command_center_customization", active=False)
    def test_endpoints_are_unavailable_when_feature_is_disabled(self):
        response = self.client.post(
            self.url("save_layout"),
            data=json.dumps(self.payload()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    @override_flag("command_center_customization", active=True)
    @override_flag("business_impact_roi", active=False)
    def test_save_reset_and_revision_conflict(self):
        payload = self.payload()
        next(panel for panel in payload["panels"] if panel["id"] == "asset_mix")["hidden"] = True
        response = self.client.post(self.url("save_layout"), data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["personal_revision"], 1)

        conflict = self.client.post(self.url("save_layout"), data=json.dumps(payload), content_type="application/json")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "revision_conflict")

        reset = self.client.post(
            self.url("reset_layout"),
            data=json.dumps({"base_revision": 1}),
            content_type="application/json",
        )
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["source"], "novena_default")
        self.assertFalse(CommandCenterLayout.objects.filter(user=self.owner).exists())

    @override_flag("command_center_customization", active=True)
    @override_flag("business_impact_roi", active=False)
    def test_publish_affects_only_users_without_personal_layouts(self):
        save = self.client.post(
            self.url("save_layout"),
            data=json.dumps(self.payload()),
            content_type="application/json",
        )
        self.assertEqual(save.status_code, 200)
        published = self.client.post(
            self.url("publish_default"),
            data=json.dumps({"base_revision": 0}),
            content_type="application/json",
        )
        self.assertEqual(published.status_code, 200)
        self.assertEqual(resolve_layout(self.team, self.viewer)["source"], CommandCenterLayout.Scope.TEAM_DEFAULT)
        self.assertEqual(resolve_layout(self.team, self.owner)["source"], CommandCenterLayout.Scope.PERSONAL)

        self.client.force_login(self.viewer)
        forbidden = self.client.post(
            self.url("remove_default"),
            data=json.dumps({"base_revision": 1}),
            content_type="application/json",
        )
        self.assertEqual(forbidden.status_code, 403)

    @override_flag("command_center_customization", active=True)
    def test_save_is_tenant_scoped(self):
        response = self.client.post(
            self.url("save_layout", self.other_team),
            data=json.dumps(self.payload()),
            content_type="application/json",
        )
        self.assertIn(response.status_code, {403, 404})

    @override_flag("command_center_customization", active=True)
    def test_save_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        response = csrf_client.post(
            self.url("save_layout"),
            data=json.dumps(self.payload()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    @override_flag("command_center_customization", active=True)
    def test_revision_must_be_a_non_negative_integer(self):
        payload = self.payload()
        payload["base_revision"] = "0"
        response = self.client.post(
            self.url("save_layout"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_layout")


class CommandCenterRenderingTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Pilot Cold Room", slug="pilot-cold-room")
        self.user = CustomUser.objects.create_user(
            username="operator@example.com",
            email="operator@example.com",
            password="testpass123",
        )
        Membership.objects.create(team=self.team, user=self.user, role=ROLE_OWNER)
        site = Site.objects.create(team=self.team, name="Cold Room A")
        Device.objects.create(
            team=self.team,
            site=site,
            name="Temperature Sensor",
            device_type="temp_sensor",
            protocol="mqtt",
        )
        self.client.force_login(self.user)

    @override_flag("command_center_customization", active=True)
    @override_flag("business_impact_roi", active=False)
    def test_single_device_dashboard_focuses_on_equipment(self):
        response = self.client.get(reverse("web_team:home", args=[self.team.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Equipment overview"')
        self.assertNotContains(response, "data-fixed-kpi-strip")
        self.assertContains(response, 'id="command-center-customize"')
        self.assertContains(response, 'id="command-center-grid" class="grid-stack"')
        self.assertContains(response, 'data-panel-id="needs_attention"')
        self.assertContains(response, 'id="command-center-panel-drawer"')
        self.assertNotContains(response, 'data-panel-id="business_impact"')
        panels = {panel["id"]: panel for panel in response.context["command_center_panels"]}
        for panel_id in ("asset_mix", "device_fleet", "sites_actions"):
            self.assertTrue(panels[panel_id]["hidden"])

    @override_flag("command_center_customization", active=False)
    def test_disabled_dashboard_keeps_static_fallback(self):
        response = self.client.get(reverse("web_team:home", args=[self.team.slug]))
        self.assertContains(response, 'id="command-center-grid" class="command-center-static-grid"')
        self.assertNotContains(response, 'id="command-center-customize"')
