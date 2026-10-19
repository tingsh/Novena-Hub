import ast
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from apps.devices.management.commands.seed_pilot_cold_room_simulator_template import REGISTER_MAP, TEMPLATE_NAME
from apps.devices.models import DeviceTemplate
from apps.devices.services import visible_templates_for_team
from apps.teams.models import Team


def simulator_addresses():
    source = Path(__file__).resolve().parents[3] / "scripts" / "modbus_simulator.py"
    module = ast.parse(source.read_text())
    return {
        target.id: ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"COLD_ROOM_REGISTERS", "COLD_ROOM_COILS"}
    }


class SeedPilotColdRoomSimulatorTemplateTest(TestCase):
    def test_matches_tcp_simulator_and_remains_private_unverified(self):
        pilot = Team.objects.create(name="Pilot", slug="pilot")
        other = Team.objects.create(name="Other", slug="other")

        call_command("seed_pilot_cold_room_simulator_template", team=pilot.slug, stdout=StringIO())
        call_command("seed_pilot_cold_room_simulator_template", team=pilot.slug, stdout=StringIO())

        template = DeviceTemplate.objects.get(name=TEMPLATE_NAME)
        self.assertEqual(DeviceTemplate.objects.count(), 1)
        self.assertEqual(template.created_by_team, pilot)
        self.assertEqual(template.protocol, "modbus_tcp")
        self.assertEqual(template.mapping_strategy, "fixed")
        self.assertFalse(template.is_verified)
        self.assertEqual(template.alert_presets, [])
        self.assertTrue(visible_templates_for_team(pilot).filter(pk=template.pk).exists())
        self.assertFalse(visible_templates_for_team(other).filter(pk=template.pk).exists())
        addresses = simulator_addresses()
        for signal, address in addresses["COLD_ROOM_REGISTERS"].items():
            self.assertEqual(REGISTER_MAP[signal]["address"], address)
            self.assertEqual(REGISTER_MAP[signal]["functionCode"], 3)
            self.assertEqual(REGISTER_MAP[signal]["type"], "float32")
        for signal, address in addresses["COLD_ROOM_COILS"].items():
            self.assertEqual(REGISTER_MAP[signal]["address"], address)
            self.assertEqual(REGISTER_MAP[signal]["functionCode"], 1)
            self.assertEqual(REGISTER_MAP[signal]["type"], "bool")
