import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage, storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.devices.config_generator import generate_connector_config
from apps.devices.datapoint_maps import (
    confirm_device_datapoint_map,
    ensure_device_datapoint_map,
    mapping_checksum,
    record_device_datapoint_validation,
    reset_device_datapoint_map_for_template,
    save_device_datapoint_map,
)
from apps.devices.models import Device, DeviceTemplate, EquipmentTemplateRequest, Gateway, Site
from apps.devices.services import visible_templates_for_team
from apps.devices.team_maps import save_confirmed_team_map
from apps.devices.template_help import TemplateHelpRequestForm, create_help_request, deliver_help_map
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_ADMIN
from apps.users.models import CustomUser


class TemplateSystemV2Test(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Energy team", slug="energy-team")
        self.user = CustomUser.objects.create(email="energy@example.com", username="energy")
        Membership.objects.create(team=self.team, user=self.user, role=ROLE_ADMIN)
        self.site = Site.objects.create(team=self.team, name="Plant")
        self.gateway = Gateway.objects.create(
            team=self.team, site=self.site, name="Gateway", serial_number="NF-ENERGY-1",
            access_token="energy-token",
        )
        self.starter = DeviceTemplate.objects.create(
            name="Meter starter", manufacturer="Example", model_number="M1",
            device_type="power_meter", protocol="modbus_tcp", mapping_strategy="site_defined",
            register_map={}, source="user_created", created_by_team=self.team,
        )
        self.device = Device.objects.create(
            team=self.team, site=self.site, gateway=self.gateway, template=self.starter,
            name="Main meter", device_type="power_meter", protocol="modbus_tcp",
            port="10.1.1.2:502", connection_config={"host": "10.1.1.2", "port": 502, "slave_id": 1},
        )
        self.point = {
            "key": "active_power", "label": "Active power", "address": 101,
            "read_function_code": 3, "data_type": "float32", "objects_count": 2,
            "unit": "W", "multiplier": 1, "access": "read_only",
        }

    def confirm(self, points=None):
        mapping = save_device_datapoint_map(
            device=self.device, team=self.team, datapoints=points or [self.point],
        )
        checksum = mapping_checksum(self.device, mapping.datapoints)
        record_device_datapoint_validation(
            device=self.device,
            result={
                "status": "success", "mapping_checksum": checksum,
                "signals": [
                    {"key": point["key"], "status": "success", "address": point["address"], "value": 50}
                    for point in mapping.datapoints
                ],
            },
        )
        return confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)

    def test_confirmed_map_becomes_private_revision_and_reuse_is_draft(self):
        self.confirm()
        first = save_confirmed_team_map(
            device=self.device, team=self.team, name="M1 power", manufacturer="Example",
            model_number="M1", configuration_notes="CT ratio 100:5",
        )
        self.assertEqual(first.catalog_revision, 1)
        self.assertIn("CT ratio", first.compatibility_notes)
        self.assertNotIn("10.1.1.2", str(first.register_map))
        self.assertEqual(save_confirmed_team_map(
            device=self.device, team=self.team, name="M1 power", manufacturer="Example", model_number="M1",
        ).pk, first.pk)
        other_team = Team.objects.create(name="Other", slug="other-template-team")
        self.assertFalse(visible_templates_for_team(other_team).filter(pk=first.pk).exists())

        second_device = Device.objects.create(
            team=self.team, site=self.site, gateway=self.gateway, template=first,
            name="Second meter", device_type="power_meter", protocol="modbus_tcp",
            connection_config={"host": "10.1.1.3", "port": 502, "slave_id": 1},
        )
        seeded = ensure_device_datapoint_map(second_device)
        self.assertEqual(seeded.status, "draft")
        self.assertEqual(seeded.datapoints[0]["address"], 101)
        self.assertFalse(seeded.confirmed_checksum)

        self.confirm([{**self.point, "address": 102}])
        revised = save_confirmed_team_map(
            device=self.device, team=self.team, name="M1 power", manufacturer="Example", model_number="M1",
        )
        first.refresh_from_db()
        self.assertEqual(revised.catalog_revision, 2)
        self.assertFalse(first.is_current)
        self.assertEqual(second_device.template_id, first.pk)
        with self.assertRaises(ValidationError):
            first.register_map = revised.register_map
            first.save()

    def test_switching_team_map_clears_old_confirmation_and_writes_cannot_be_shared(self):
        self.confirm()
        saved = save_confirmed_team_map(
            device=self.device, team=self.team, name="M1", manufacturer="Example", model_number="M1",
        )
        self.device.template = saved
        self.device.save(update_fields=["template"])
        reset = reset_device_datapoint_map_for_template(device=self.device, team=self.team)
        self.assertEqual(reset.status, "draft")
        self.assertFalse(reset.confirmed_checksum)
        self.confirm([{**self.point, "access": "read_write"}])
        with self.assertRaises(ValidationError):
            save_confirmed_team_map(
                device=self.device, team=self.team, name="M1", manufacturer="Example", model_number="M1",
            )

    def test_invalid_codes_and_energy_direction_survive_gateway_config(self):
        self.confirm([{**self.point, "invalid_values": [65535], "energy_direction": "import"}])
        config = generate_connector_config(self.gateway)
        point = config[0]["config"]["master"]["slaves"][0]["timeseries"][0]
        self.assertEqual(point["invalidValues"], [65535])
        saved = save_confirmed_team_map(
            device=self.device, team=self.team, name="M1 import", manufacturer="Example", model_number="M1",
        )
        self.assertEqual(saved.register_map["active_power"]["energy_direction"], "import")

    def test_paid_request_requires_quote_payment_and_private_delivery(self):
        form = TemplateHelpRequestForm({
            "manufacturer": "Example", "model_number": "M2", "protocol": "modbus_tcp",
            "device_type": "power_meter", "equipment_purpose": "Main energy meter",
            "desired_signals": "Active power",
        })
        help_request, created = create_help_request(team=self.team, actor=self.user, form=form)
        self.assertTrue(created)
        self.assertEqual(help_request.status, "submitted")
        again, created = create_help_request(team=self.team, actor=self.user, form=form)
        self.assertFalse(created)
        self.assertEqual(again.pk, help_request.pk)

        help_request.status = EquipmentTemplateRequest.Status.QUOTED
        with self.assertRaises(ValidationError):
            help_request.full_clean()
        help_request.quote_reference = "Q-101"
        help_request.quote_amount = 250
        help_request.full_clean()
        help_request.save()
        help_request.status = EquipmentTemplateRequest.Status.IN_PROGRESS
        with self.assertRaises(ValidationError):
            help_request.full_clean()
        help_request.quote_accepted_reference = "EMAIL-ACCEPT-101"
        help_request.payment_reference = "INV-101"
        help_request.full_clean()
        help_request.save()
        help_request.proposed_signals = [self.point]
        help_request.save(update_fields=["proposed_signals"])
        with self.assertRaises(ValidationError):
            deliver_help_map(help_request=help_request, actor=self.user)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        delivered = deliver_help_map(help_request=help_request, actor=self.user)
        help_request.refresh_from_db()
        self.assertEqual(help_request.status, "ready_to_test")
        self.assertEqual(delivered.created_by_team_id, self.team.pk)
        self.assertEqual(delivered.source, "novena_assisted")
        self.assertFalse(delivered.is_verified)
        self.assertEqual(deliver_help_map(help_request=help_request, actor=self.user).pk, delivered.pk)
        other = Team.objects.create(name="Unrelated", slug="unrelated-template-help")
        self.assertFalse(visible_templates_for_team(other).filter(pk=delivered.pk).exists())
        help_request.status = EquipmentTemplateRequest.Status.COMPLETED
        with self.assertRaises(ValidationError):
            help_request.full_clean()

    def test_request_pages_are_team_scoped(self):
        self.client.force_login(self.user)
        url = reverse("web_team:devices:template_help_list", args=[self.team.slug])
        response = self.client.post(url, {
            "manufacturer": "Example", "model_number": "M2", "protocol": "modbus_tcp",
            "device_type": "power_meter", "equipment_purpose": "Energy metering",
            "desired_signals": "Power and energy",
        })
        self.assertEqual(response.status_code, 302)
        help_request = EquipmentTemplateRequest.objects.get(team=self.team)
        detail = reverse("web_team:devices:template_help_detail", args=[self.team.slug, help_request.pk])
        self.assertContains(self.client.get(detail), str(help_request.support_reference))
        other = Team.objects.create(name="Other", slug="other-help-team")
        Membership.objects.create(team=other, user=self.user, role=ROLE_ADMIN)
        other_url = reverse("web_team:devices:template_help_detail", args=[other.slug, help_request.pk])
        self.assertEqual(self.client.get(other_url).status_code, 404)

    def test_manual_file_is_private_and_download_is_team_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage_settings = {
                **settings.STORAGES,
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": root / "public"},
                },
                "template_request_documents": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": root / "private", "base_url": None},
                },
            }
            with override_settings(STORAGES=storage_settings, MEDIA_ROOT=root / "public"):
                field = EquipmentTemplateRequest._meta.get_field("documentation_file")
                with patch.object(field, "storage", storages["template_request_documents"]):
                    self.client.force_login(self.user)
                    url = reverse("web_team:devices:template_help_list", args=[self.team.slug])
                    response = self.client.post(url, {
                        "manufacturer": "Example", "model_number": "M2", "protocol": "modbus_tcp",
                        "device_type": "power_meter", "equipment_purpose": "Energy metering",
                        "desired_signals": "Power and energy",
                        "documentation_file": SimpleUploadedFile("manual.pdf", b"%PDF-1.4\nprivate", "application/pdf"),
                    })
                self.assertEqual(response.status_code, 302)
                help_request = EquipmentTemplateRequest.objects.get(team=self.team)
                self.assertTrue(storages["template_request_documents"].exists(help_request.documentation_file.name))
                self.assertFalse(storages["default"].exists(help_request.documentation_file.name))
                download = reverse(
                    "web_team:devices:template_help_file", args=[self.team.slug, help_request.pk, "manual"],
                )
                self.assertEqual(self.client.get(download).status_code, 200)
                other = Team.objects.create(name="Other", slug="other-private-docs")
                Membership.objects.create(team=other, user=self.user, role=ROLE_ADMIN)
                other_download = reverse(
                    "web_team:devices:template_help_file", args=[other.slug, help_request.pk, "manual"],
                )
                self.assertEqual(self.client.get(other_download).status_code, 404)

    def test_legacy_public_manual_migration_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage_settings = {
                **settings.STORAGES,
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": root / "public"},
                },
                "template_request_documents": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": root / "private", "base_url": None},
                },
            }
            with override_settings(STORAGES=storage_settings, MEDIA_ROOT=root / "public"):
                path = "equipment-template-requests/legacy.pdf"
                default_storage.save(path, ContentFile(b"%PDF-1.4\nold"))
                help_request = EquipmentTemplateRequest.objects.create(
                    team=self.team, requested_by=self.user, manufacturer="Example",
                    model_number="M1", protocol="modbus_tcp", documentation_file=path,
                )
                self.assertEqual(help_request.documentation_file.name, path)
                call_command("migrate_template_request_files")
                self.assertTrue(default_storage.exists(path))
                call_command("migrate_template_request_files", "--apply")
                self.assertFalse(default_storage.exists(path))
                self.assertTrue(storages["template_request_documents"].exists(path))
                call_command("migrate_template_request_files", "--apply")
