import copy
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.template.loader import render_to_string
from django.test import TestCase

from apps.devices.config_generator import generate_connector_config
from apps.devices.models import Device, DeviceTemplate, Gateway, Site
from apps.devices.remote_control import CommandDenied, resolve_device_params
from apps.devices.services import visible_templates_for_team
from apps.teams.models import Team


class ImportEnergyCatalogTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Energy Pilot", slug="energy-pilot")
        self.site = Site.objects.create(team=self.team, name="Plant")
        self.gateway = Gateway.objects.create(
            team=self.team, site=self.site, name="Gateway", serial_number="NF-ENERGY-001"
        )

    def _import(self, **options):
        output = StringIO()
        call_command("import_energy_catalog", stdout=output, **options)
        return output.getvalue()

    def test_repeatable_import_retires_legacy_without_moving_assigned_device(self):
        legacy = DeviceTemplate.objects.create(
            name="Schneider PM5350",
            manufacturer="Schneider Electric",
            model_number="PM5350",
            device_type="power_meter",
            protocol="modbus_tcp",
            category="energy",
            register_map={"active_power": {"address": 3054, "type": "float32", "functionCode": 3}},
            is_verified=True,
        )
        assigned = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Installed meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            template=legacy,
        )
        self.assertIn("1 assigned device(s)", self._import(dry_run=True))
        legacy.refresh_from_db()
        self.assertTrue(legacy.is_current)
        self.assertEqual(DeviceTemplate.objects.filter(catalog_key__isnull=False).count(), 0)

        self._import()
        self._import()
        legacy.refresh_from_db()
        assigned.refresh_from_db()
        self.assertEqual(assigned.template_id, legacy.id)
        self.assertEqual(legacy.register_map["active_power"]["address"], 3054)
        self.assertFalse(legacy.is_current)
        self.assertFalse(legacy.is_verified)
        self.assertEqual(legacy.evidence_status, "retired")
        self.assertFalse(visible_templates_for_team(self.team).filter(pk=legacy.pk).exists())
        self.assertEqual(DeviceTemplate.objects.filter(catalog_key__isnull=False).count(), 7)
        self.assertEqual(visible_templates_for_team(self.team).filter(category="energy").count(), 7)
        self.assertFalse(DeviceTemplate.objects.filter(catalog_key__isnull=False, is_verified=True).exists())

    def test_catalog_map_generates_read_only_gateway_config_and_denies_write(self):
        self._import()
        template = DeviceTemplate.objects.get(catalog_key="abb-b23-312-100-modbus-rtu")
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="ABB main meter",
            device_type="power_meter",
            protocol="modbus_rtu",
            template=template,
            discovery_meta={"interface": "/dev/ttyUSB0", "slave_id": 11},
        )
        slave = generate_connector_config(self.gateway)[0]["config"]["master"]["slaves"][0]
        self.assertEqual(slave["deviceId"], str(device.pk))
        self.assertEqual(slave["type"], "serial")
        self.assertEqual(slave["wordOrder"], "BIG")
        self.assertEqual(slave["unitId"], 11)
        points = {point["tag"]: point for point in slave["timeseries"]}
        self.assertEqual(points["voltage_l1_n"]["address"], 0x5B00)
        self.assertEqual(points["voltage_l1_n"]["type"], "32uint")
        self.assertEqual(points["voltage_l1_n"]["multiplier"], 0.1)
        self.assertEqual(points["voltage_l1_n"]["invalidValues"], [0xFFFFFFFF])
        self.assertEqual(points["energy_import"]["objectsCount"], 4)
        self.assertEqual(points["energy_import"]["invalidValues"], [0xFFFFFFFFFFFFFFFF])
        self.assertFalse(slave["attributes"])
        self.assertTrue(all(point["functionCode"] in (3, 4) for point in points.values()))
        with self.assertRaises(CommandDenied):
            resolve_device_params(device, "active_power_total", "write_device", 1)

    def test_changed_published_map_requires_new_revision(self):
        self._import()
        from apps.devices.management.commands import import_energy_catalog

        payload = json.loads(import_energy_catalog.MANIFEST.read_text())
        payload["templates"][0]["register_map"]["voltage_l1_n"]["address"] += 1
        with TemporaryDirectory() as directory:
            altered = Path(directory) / "altered.json"
            altered.write_text(json.dumps(payload))
            with (
                patch.object(import_energy_catalog, "MANIFEST", altered),
                self.assertRaisesMessage(CommandError, "Create a new revision"),
            ):
                self._import()
        self.assertEqual(
            DeviceTemplate.objects.get(catalog_key="schneider-pm5110-modbus-rtu").register_map["voltage_l1_n"][
                "address"
            ],
            3027,
        )

    def test_guided_picker_displays_model_status_revision_and_oem_source(self):
        self._import()
        template = DeviceTemplate.objects.get(catalog_key="schneider-pm5110-modbus-rtu")
        html = render_to_string(
            "onboarding/partials/template_search_results.html",
            {
                "candidate_index": "1",
                "template_count": 1,
                "templates": [template],
            },
        )
        self.assertIn("METSEPM5110", html)
        self.assertIn("Documented draft", html)
        self.assertIn("OEM register source", html)
        self.assertIn("r1", html)

    def test_oem_register_coordinates_are_preserved(self):
        self._import()
        expected = {
            "schneider-pm5110-modbus-rtu": ("active_power_total", 3059, 3, "float32", 1000),
            "schneider-pm5340-modbus-tcp": ("energy_import", 2699, 3, "float32", 1),
            "schneider-iem3255-modbus-rtu": ("energy_import", 3203, 3, "int64", 1),
            "eastron-sdm120-m-modbus-rtu": ("energy_export", 74, 4, "float32", 1),
            "siemens-pac3220-7km3220-0ba01-1da0-modbus-tcp": ("active_power_total", 64, 3, "float32", 1),
            "abb-b23-312-100-modbus-rtu": ("energy_import", 0x5000, 3, "uint64", 0.01),
            "socomec-diris-a40-48250501-modbus-tcp": ("frequency", 18442, 3, "uint32", 0.001),
        }
        for key, (signal_name, address, function_code, data_type, scale) in expected.items():
            with self.subTest(model=key):
                signal = DeviceTemplate.objects.get(catalog_key=key).register_map[signal_name]
                self.assertEqual(
                    (signal["address"], signal["functionCode"], signal["type"], signal["scale"]),
                    (address, function_code, data_type, scale),
                )

    def test_new_revision_preserves_existing_device_pin(self):
        self._import()
        from apps.devices.management.commands import import_energy_catalog

        first = DeviceTemplate.objects.get(catalog_key="eastron-sdm120-m-modbus-rtu")
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Existing SDM120",
            device_type="power_meter",
            protocol="modbus_rtu",
            template=first,
        )
        payload = json.loads(import_energy_catalog.MANIFEST.read_text())
        second = copy.deepcopy(next(item for item in payload["templates"] if item["catalog_key"] == first.catalog_key))
        second["catalog_revision"] = 2
        second["name"] += " revision 2"
        second["register_map"]["voltage"]["scale"] = 1.0
        payload["templates"].append(second)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "next.json"
            path.write_text(json.dumps(payload))
            with patch.object(import_energy_catalog, "MANIFEST", path):
                self._import()
        first.refresh_from_db()
        device.refresh_from_db()
        self.assertFalse(first.is_current)
        self.assertEqual(device.template_id, first.id)
        self.assertTrue(DeviceTemplate.objects.get(catalog_key=first.catalog_key, catalog_revision=2).is_current)
        self.assertFalse(visible_templates_for_team(self.team).filter(pk=first.pk).exists())
