import json
from importlib import import_module

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.devices.models import (
    DeploymentSetupItem,
    DeploymentSetupRun,
    Device,
    DeviceTemplate,
    Gateway,
    PrivateTemplateDraft,
    SavedDeviceTemplate,
    Site,
    TemplateShareSubmission,
)
from apps.devices.services import visible_templates_for_team
from apps.devices.template_sharing import publish_reviewed_submission
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_ADMIN
from apps.users.models import CustomUser


class DeviceTemplateLibraryTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Plant", slug="plant-library")
        self.user = CustomUser.objects.create(email="library@example.com", username="library-user")
        Membership.objects.create(team=self.team, user=self.user, role=ROLE_ADMIN)
        self.client.force_login(self.user)
        self.template = DeviceTemplate.objects.create(
            name="PowerLogic PM5110", manufacturer="Schneider Electric", model_number="PM5110",
            model_aliases=["METSEPM5110"], device_type="power_meter", protocol="modbus_rtu",
            category="energy", register_map={"active_power": {
                "address": 3054, "functionCode": 3, "type": "float32", "unit": "W", "multiplier": 1,
            }}, evidence_status="documented", source="curated", source_url="https://example.com/pm5110",
            catalog_key="pm5110", catalog_revision=1,
        )

    def test_search_detail_and_save_work_without_gateway(self):
        url = reverse("web_team:devices:template_library", args=[self.team.slug])
        response = self.client.get(url, {"q": "metsepm5110"})
        self.assertContains(response, "PM5110")
        self.assertContains(response, "Schneider Electric")
        self.assertContains(self.client.get(url, {"q": "METSE-PM5110"}), "PM5110")
        response = self.client.get(url, {"q": "PM-5110", "protocol": "modbus_rtu"})
        self.assertContains(response, "PM5110")
        detail_url = reverse("web_team:devices:template_detail", args=[self.team.slug, self.template.pk])
        self.assertContains(self.client.get(detail_url), "Live testing becomes available after your Gateway is claimed")
        save_url = reverse("web_team:devices:template_save", args=[self.team.slug, self.template.pk])
        self.assertEqual(self.client.post(save_url).status_code, 302)
        self.assertTrue(SavedDeviceTemplate.objects.filter(team=self.team, template=self.template).exists())
        self.assertFalse(Device.objects.exists())

    def test_saved_revision_is_prioritized_in_guided_setup_picker(self):
        for number in range(12):
            DeviceTemplate.objects.create(
                name=f"Example meter {number}", manufacturer="Example", model_number=f"E{number}",
                device_type="power_meter", protocol="modbus_rtu", register_map={},
                source="curated", evidence_status="legacy_unreviewed",
            )
        SavedDeviceTemplate.objects.create(team=self.team, template=self.template, saved_by=self.user)
        url = reverse("web_team:devices:template_library_search", args=[self.team.slug])
        response = self.client.get(url, {
            "q": "", "context": "guided_setup", "protocol": "modbus_rtu", "candidate_index": "1",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Saved for setup")
        self.assertLess(response.content.index(b"PowerLogic PM5110"), response.content.index(b"Example meter 0"))

    def test_retired_saved_revision_warns_and_can_be_removed(self):
        SavedDeviceTemplate.objects.create(team=self.team, template=self.template, saved_by=self.user)
        self.template.is_current = False
        self.template.evidence_status = "retired"
        self.template.save(update_fields=["is_current", "evidence_status"])
        url = reverse("web_team:devices:template_library", args=[self.team.slug])
        self.assertContains(self.client.get(url), "Older or retired revision")
        remove = reverse("web_team:devices:template_save", args=[self.team.slug, self.template.pk])
        self.assertEqual(self.client.post(remove, {"action": "remove"}).status_code, 302)
        self.assertFalse(SavedDeviceTemplate.objects.filter(team=self.team).exists())

    def test_private_draft_before_gateway_requires_canonical_read_signals_for_publication(self):
        url = reverse("web_team:devices:template_draft_new", args=[self.team.slug])
        data = {
            "name": "Plant meter", "manufacturer": "Example", "model_number": "M-1",
            "protocol": "modbus_tcp", "device_type": "power_meter", "category": "energy",
            "signals_json": json.dumps([{
                "key": "active_power", "label": "Active power", "address": 100,
                "read_function_code": 3, "data_type": "float32", "unit": "W",
                "multiplier": 1, "access": "read_only",
            }]),
        }
        self.assertEqual(self.client.post(url, {**data, "action": "save"}).status_code, 302)
        draft = PrivateTemplateDraft.objects.get(team=self.team)
        self.assertFalse(DeviceTemplate.objects.filter(created_by_team=self.team).exists())
        edit = reverse("web_team:devices:template_draft_edit", args=[self.team.slug, draft.pk])
        self.assertEqual(self.client.post(edit, {**data, "action": "publish"}).status_code, 302)
        published = DeviceTemplate.objects.get(created_by_team=self.team)
        self.assertEqual(published.catalog_revision, 1)
        self.assertEqual(published.evidence_status, "legacy_unreviewed")
        self.assertFalse(published.register_map["active_power"]["writable"])
        other = Team.objects.create(name="Other", slug="other-library")
        self.assertFalse(visible_templates_for_team(other).filter(pk=published.pk).exists())
        self.client.post(edit, {**data, "name": "Plant meter revised", "action": "publish"})
        published.refresh_from_db()
        self.assertFalse(published.is_current)
        self.assertEqual(DeviceTemplate.objects.get(created_by_team=self.team, is_current=True).catalog_revision, 2)

    def test_private_publication_rejects_write_capable_signal(self):
        url = reverse("web_team:devices:template_draft_new", args=[self.team.slug])
        response = self.client.post(url, {
            "name": "Control", "manufacturer": "Example", "model_number": "M2",
            "protocol": "modbus_tcp", "device_type": "power_meter", "category": "energy",
            "signals_json": json.dumps([{
                "key": "setpoint", "address": 10, "data_type": "uint16", "access": "read_write",
            }]), "action": "publish",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DeviceTemplate.objects.filter(created_by_team=self.team).exists())

    def test_handoff_selects_specific_equipment_without_testing_or_deployment(self):
        site = Site.objects.create(team=self.team, name="Factory")
        gateway = Gateway.objects.create(team=self.team, site=site, name="CM4", serial_number="NF-LIB-1")
        run = DeploymentSetupRun.objects.create(team=self.team, site=site, gateway=gateway)
        device = Device.objects.create(
            team=self.team, site=site, gateway=gateway, name="Main meter",
            device_type="power_meter", protocol="modbus_rtu", template=self.template,
        )
        item = DeploymentSetupItem.objects.create(team=self.team, run=run, device=device)
        use_url = reverse("web_team:devices:template_use", args=[self.team.slug, self.template.pk])
        self.assertEqual(self.client.post(use_url, {"item_id": item.pk}).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.state, DeploymentSetupItem.State.DISCOVERED)
        self.client.post(use_url, {"item_id": item.pk, "compatibility_confirmed": "yes"})
        item.refresh_from_db()
        self.assertEqual(item.selected_template_id, self.template.pk)
        self.assertEqual(item.state, DeploymentSetupItem.State.TEMPLATE_SELECTED)
        self.assertIsNone(item.validation_command_id)
        self.assertFalse(item.validation_result)

    def test_saved_discovery_can_receive_template_before_device_record_exists(self):
        site = Site.objects.create(team=self.team, name="Factory")
        gateway = Gateway.objects.create(team=self.team, site=site, name="CM4", serial_number="NF-LIB-2")
        run = DeploymentSetupRun.objects.create(team=self.team, site=site, gateway=gateway)
        item = DeploymentSetupItem.objects.create(
            team=self.team, run=run,
            candidate_data={"signature": "RS485 meter", "connection": "modbus_rtu"},
        )
        detail_url = reverse("web_team:devices:template_detail", args=[self.team.slug, self.template.pk])
        self.assertContains(self.client.get(detail_url), "RS485 meter")
        use_url = reverse("web_team:devices:template_use", args=[self.team.slug, self.template.pk])
        self.assertEqual(self.client.post(use_url, {
            "item_id": item.pk, "compatibility_confirmed": "yes",
        }).status_code, 302)
        item.refresh_from_db()
        self.assertIsNone(item.device_id)
        self.assertEqual(item.selected_template_id, self.template.pk)
        self.assertEqual(item.state, DeploymentSetupItem.State.TEMPLATE_SELECTED)

    def test_known_model_mismatch_cannot_be_overridden_by_checkbox(self):
        site = Site.objects.create(team=self.team, name="Factory")
        gateway = Gateway.objects.create(team=self.team, site=site, name="CM4", serial_number="NF-LIB-3")
        run = DeploymentSetupRun.objects.create(team=self.team, site=site, gateway=gateway)
        item = DeploymentSetupItem.objects.create(
            team=self.team, run=run, candidate_data={
                "connection": "modbus_rtu", "identification": {"model": "PM5350"},
            },
        )
        use_url = reverse("web_team:devices:template_use", args=[self.team.slug, self.template.pk])
        self.client.post(use_url, {"item_id": item.pk, "compatibility_confirmed": "yes"})
        item.refresh_from_db()
        self.assertIsNone(item.selected_template_id)

    def test_plc_program_mismatch_blocks_library_handoff(self):
        plc = DeviceTemplate.objects.create(
            name="PLC program A", manufacturer="Siemens", model_number="S7-1200",
            device_type="plc", protocol="modbus_tcp", mapping_strategy="site_defined",
            program_scope="Program A", register_map={"temperature": {
                "address": 100, "functionCode": 3, "type": "float32", "unit": "°C",
            }}, source="user_created", created_by_team=self.team,
        )
        site = Site.objects.create(team=self.team, name="Factory")
        gateway = Gateway.objects.create(team=self.team, site=site, name="CM4", serial_number="NF-LIB-4")
        run = DeploymentSetupRun.objects.create(team=self.team, site=site, gateway=gateway)
        item = DeploymentSetupItem.objects.create(
            team=self.team, run=run, candidate_data={
                "connection": "modbus_tcp",
                "identification": {"model": "S7-1200", "program": "Program B"},
            },
        )
        url = reverse("web_team:devices:template_use", args=[self.team.slug, plc.pk])
        self.client.post(url, {
            "item_id": item.pk, "compatibility_confirmed": "yes", "program_confirmed": "yes",
        })
        item.refresh_from_db()
        self.assertIsNone(item.selected_template_id)

    def test_other_team_cannot_open_or_save_private_revision(self):
        private = DeviceTemplate.objects.create(
            name="Private meter", manufacturer="Private", model_number="M2",
            device_type="power_meter", protocol="modbus_tcp", register_map={},
            source="user_created", created_by_team=self.team,
        )
        other = Team.objects.create(name="Other", slug="other-private-library")
        Membership.objects.create(team=other, user=self.user, role=ROLE_ADMIN)
        detail_url = reverse("web_team:devices:template_detail", args=[other.slug, private.pk])
        save_url = reverse("web_team:devices:template_save", args=[other.slug, private.pk])
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        self.assertEqual(self.client.post(save_url).status_code, 404)

    def test_public_seed_audit_retires_unsafe_entry_without_moving_device(self):
        site = Site.objects.create(team=self.team, name="Factory")
        legacy = DeviceTemplate.objects.create(
            name="Generic PLC", manufacturer="Example", model_number="PLC",
            device_type="plc", protocol="modbus_tcp", register_map={},
            source="curated", is_verified=True,
        )
        device = Device.objects.create(
            team=self.team, site=site, name="Installed PLC", template=legacy,
            device_type="plc", protocol="modbus_tcp",
        )
        migration = import_module("apps.devices.migrations.0038_retire_unsafe_public_templates")
        migration.retire_unsafe_public_templates(apps, None)
        legacy.refresh_from_db()
        device.refresh_from_db()
        self.assertFalse(legacy.is_current)
        self.assertEqual(legacy.evidence_status, "retired")
        self.assertEqual(device.template_id, legacy.pk)

    def test_staff_can_publish_community_documented_without_field_verified_claim(self):
        from apps.devices.models import DeviceDatapointMap, DeviceDatapointMapRevision

        site = Site.objects.create(team=self.team, name="Factory")
        device = Device.objects.create(
            team=self.team, site=site, name="Meter", template=self.template,
            device_type="power_meter", protocol="modbus_rtu",
        )
        private = DeviceTemplate.objects.create(
            name="Private M3", manufacturer="Example", model_number="M3",
            device_type="power_meter", protocol="modbus_rtu", source="user_created",
            created_by_team=self.team, register_map={},
        )
        device.template = private
        device.save(update_fields=["template"])
        point = {
            "key": "active_power", "label": "Active power", "address": 100,
            "read_function_code": 3, "data_type": "float32", "objects_count": 2,
            "unit": "W", "multiplier": 1, "offset": 0, "access": "read_only",
            "display_type": "value", "energy_direction": "none",
        }
        mapping = DeviceDatapointMap.objects.create(team=self.team, device=device, datapoints=[point])
        revision = DeviceDatapointMapRevision.objects.create(
            team=self.team, mapping=mapping, revision_number=1, datapoints=[point], datapoints_checksum="test",
        )
        from apps.devices.datapoint_maps import datapoints_to_register_map

        submission = TemplateShareSubmission.objects.create(
            team=self.team, template=private, revision=revision,
            register_map_snapshot=datapoints_to_register_map([point]),
            manufacturer="Example", model_number="M3", protocol="modbus_rtu",
            device_type="power_meter", category="energy",
            source_document_url="https://example.com/manual.pdf",
            review_notes="Addresses checked against OEM source.",
            public_compatibility_notes="M3 standard variant.",
            portability_evidence="M3 standard register list applies to this SKU.",
        )
        with self.assertRaises(ValidationError):
            publish_reviewed_submission(submission=submission, reviewer=self.user, evidence_status="documented")
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        published = publish_reviewed_submission(
            submission=submission, reviewer=self.user, evidence_status="documented"
        )
        self.assertEqual(published.source, "community")
        self.assertEqual(published.evidence_status, "documented")
        self.assertFalse(published.is_verified)
        self.assertIsNone(published.created_by_team_id)
        other = Team.objects.create(name="Outside", slug="outside-library")
        Membership.objects.create(team=other, user=self.user, role=ROLE_ADMIN)
        public_detail = reverse("web_team:devices:template_detail", args=[other.slug, published.pk])
        response = self.client.get(public_detail)
        self.assertContains(response, "Community contributed")
        self.assertNotContains(response, "Contributed by Plant")
        self.assertNotContains(response, "Submitted by library@example.com")
        submission.field_verification_evidence = "Representative M3 readings matched its front display."
        submission.save(update_fields=["field_verification_evidence"])
        promoted = publish_reviewed_submission(
            submission=submission, reviewer=self.user, evidence_status="field_verified"
        )
        published.refresh_from_db()
        self.assertEqual(promoted.catalog_key, published.catalog_key)
        self.assertEqual(promoted.catalog_revision, 2)
        self.assertTrue(promoted.is_verified)
        self.assertFalse(published.is_current)
        self.assertEqual(published.evidence_status, "documented")
        published.name = "Edited in place"
        with self.assertRaises(ValidationError):
            published.save(update_fields=["name"])
