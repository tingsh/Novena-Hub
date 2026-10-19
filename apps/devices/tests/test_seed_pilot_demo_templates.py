from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.devices.management.commands.seed_pilot_demo_templates import DEMO_TEMPLATES
from apps.devices.models import DeviceTemplate
from apps.devices.services import visible_templates_for_team
from apps.teams.models import Team


class SeedPilotDemoTemplatesTest(TestCase):
    def test_creates_private_unverified_starters_and_is_idempotent(self):
        pilot = Team.objects.create(name="Pilot", slug="pilot")
        other = Team.objects.create(name="Other", slug="other")

        output = StringIO()
        call_command("seed_pilot_demo_templates", team=pilot.slug, stdout=output)
        call_command("seed_pilot_demo_templates", team=pilot.slug, stdout=output)

        templates = list(visible_templates_for_team(pilot))
        self.assertEqual(len(templates), len(DEMO_TEMPLATES))
        self.assertFalse(visible_templates_for_team(other).exists())
        self.assertIn("0 demo starter(s) added", output.getvalue())
        for template in templates:
            self.assertEqual(template.created_by_team, pilot)
            self.assertEqual(template.protocol, "modbus_tcp")
            self.assertEqual(template.mapping_strategy, "site_defined")
            self.assertEqual(template.register_map, {})
            self.assertFalse(template.is_verified)
            self.assertEqual(template.source, "user_created")

    def test_missing_team_does_not_create_templates(self):
        with self.assertRaisesMessage(CommandError, "does not exist"):
            call_command("seed_pilot_demo_templates", team="missing", stdout=StringIO())
        self.assertFalse(DeviceTemplate.objects.exists())
