import ast
from pathlib import Path

from django.test import SimpleTestCase

from apps.devices.management.commands.pilot_readiness_audit import TEMPLATE_SPECS


def simulator_source():
    source = Path(__file__).resolve().parents[3] / "scripts" / "modbus_simulator.py"
    return ast.parse(source.read_text())


class FacilitiesSimulatorMapTest(SimpleTestCase):
    def test_chiller_float_registers_do_not_overlap_and_match_audit_map(self):
        module = simulator_source()
        addresses = next(
            ast.literal_eval(node.value)
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "CHILLER_REGISTERS" for target in node.targets)
        )
        used = set()
        for signal, address in addresses.items():
            span = {address, address + 1}  # Each reading is a 32-bit float.
            self.assertFalse(used & span, f"{signal} overlaps another chiller reading")
            used.update(span)
            self.assertEqual(TEMPLATE_SPECS["chiller"]["register_map"][signal]["address"], address)

    def test_facilities_discovery_identifies_chiller(self):
        module = simulator_source()
        identity_function = next(
            node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "make_identity"
        )
        products = next(
            ast.literal_eval(node.value)
            for node in identity_function.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "products" for target in node.targets)
        )
        self.assertEqual(products["facilities"][0], "NCH-200")
