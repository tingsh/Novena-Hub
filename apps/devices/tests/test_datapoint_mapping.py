from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.dashboard.services import generate_default_dashboard
from apps.devices.config_generator import generate_connector_config
from apps.devices.datapoint_maps import (
    clone_device_datapoint_map,
    confirm_device_datapoint_map,
    datapoints_from_csv,
    datapoints_to_csv,
    effective_register_map,
    mapping_checksum,
    record_device_datapoint_validation,
    register_map_to_datapoints,
    rollback_device_datapoint_map,
    save_device_datapoint_map,
)
from apps.devices.models import (
    Device,
    DeviceDatapointMap,
    DeviceDatapointMapRevision,
    DeviceTemplate,
    Gateway,
    Site,
    TemplateShareSubmission,
)
from apps.devices.services import visible_templates_for_team
from apps.devices.template_sharing import publish_verified_submission, submit_mapping_for_review
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_ADMIN
from apps.users.models import CustomUser


class DeviceDatapointMappingTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="PLC Team", slug="plc-team")
        self.user = CustomUser.objects.create(email="plc@example.com", username="plc-user")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Gateway",
            serial_number="NF-PLC-001",
            access_token="token",
        )
        self.starter = DeviceTemplate.objects.create(
            name="Siemens S7-1200 Modbus TCP",
            manufacturer="Siemens",
            model_number="S7-1200",
            device_type="plc",
            protocol="modbus_tcp",
            mapping_strategy="site_defined",
            register_map={},
            is_verified=True,
        )
        self.device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            template=self.starter,
            name="Line PLC",
            device_type="plc",
            protocol="modbus_tcp",
            port="192.168.1.50:502",
            connection_config={
                "host": "192.168.1.50",
                "port": 502,
                "slave_id": 1,
                "byteOrder": "BIG",
                "wordOrder": "BIG",
                "requested_polling_interval": 5,
            },
        )
        self.datapoints = [
            {
                "key": "process_temperature",
                "label": "Process Temperature",
                "address": 100,
                "read_function_code": 3,
                "data_type": "float32",
                "objects_count": 2,
                "unit": "°C",
                "multiplier": 0.1,
                "offset": -5,
                "access": "read_only",
                "display_type": "trend",
                "normal_min": 20,
                "normal_max": 80,
                "safety_min": -50,
                "safety_max": 200,
                "alert_suggestion": "Warn above 80 °C",
            }
        ]

    def _save_and_confirm(self):
        mapping = save_device_datapoint_map(device=self.device, team=self.team, datapoints=self.datapoints)
        checksum = mapping_checksum(self.device, mapping.datapoints)
        mapping = record_device_datapoint_validation(
            device=self.device,
            result={
                "status": "success",
                "mode": "datapoints",
                "mapping_checksum": checksum,
                "signals": [
                    {
                        "key": "process_temperature",
                        "status": "success",
                        "address": 100,
                        "sample": [16830, 0],
                        "value": 20.0,
                        "unit": "°C",
                    }
                ],
            },
        )
        return confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)

    def _private_starter(self):
        self.starter.source = "user_created"
        self.starter.created_by_team = self.team
        self.starter.is_verified = False
        self.starter.save(update_fields=["source", "created_by_team", "is_verified"])

    def test_customer_map_is_private_and_share_requires_confirmed_readings_and_consent(self):
        self._private_starter()
        other = Team.objects.create(name="Other", slug="other-mapping")
        self.assertFalse(visible_templates_for_team(other).filter(pk=self.starter.pk).exists())
        with self.assertRaises(ValidationError):
            submit_mapping_for_review(device=self.device, team=self.team, actor=self.user, consent=True)
        self._save_and_confirm()
        with self.assertRaises(ValidationError):
            submit_mapping_for_review(device=self.device, team=self.team, actor=self.user, consent=False)
        with self.assertRaises(ValidationError):
            submit_mapping_for_review(device=self.device, team=other, actor=self.user, consent=True)

        submission = submit_mapping_for_review(
            device=self.device,
            team=self.team,
            actor=self.user,
            consent=True,
            source_document_url="https://manufacturer.example/manual",
        )
        self.assertEqual(submission.status, TemplateShareSubmission.Status.SUBMITTED)
        self.assertEqual(submission.register_map_snapshot["process_temperature"]["address"], 100)
        self.assertNotIn("192.168.1.50", str(submission.register_map_snapshot))
        self.assertNotIn("sample", str(submission.register_map_snapshot))
        self.assertFalse(visible_templates_for_team(other).filter(pk=self.starter.pk).exists())
        with self.assertRaises(ValidationError):
            submit_mapping_for_review(device=self.device, team=self.team, actor=self.user, consent=True)

    def test_review_publication_requires_staff_and_evidence_and_preserves_private_original(self):
        self._private_starter()
        self._save_and_confirm()
        submission = submit_mapping_for_review(
            device=self.device,
            team=self.team,
            actor=self.user,
            consent=True,
            source_document_url="https://manufacturer.example/manual",
        )
        with self.assertRaises(ValidationError):
            publish_verified_submission(submission=submission, reviewer=self.user)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        with self.assertRaises(ValidationError):
            publish_verified_submission(submission=submission, reviewer=self.user)
        submission.review_notes = "Register addresses and units checked against OEM manual."
        submission.public_compatibility_notes = "S7-1200 with approved program revision A."
        submission.portability_evidence = "Same model and firmware register layout confirmed."
        submission.field_verification_evidence = "Representative unit readings matched the panel."
        submission.save()
        published = publish_verified_submission(submission=submission, reviewer=self.user)
        other = Team.objects.create(name="Other", slug="other-published")
        self.assertTrue(visible_templates_for_team(other).filter(pk=published.pk).exists())
        self.assertFalse(visible_templates_for_team(other).filter(pk=self.starter.pk).exists())
        self.assertEqual(published.evidence_status, "field_verified")
        self.assertEqual(published.catalog_revision, 1)
        self.assertEqual(published.compatibility_notes, submission.public_compatibility_notes)
        self.assertFalse(published.register_map["process_temperature"]["writable"])
        self.assertNotIn("normal_min", published.register_map["process_temperature"])
        self.assertNotIn("alert_suggestion", published.register_map["process_temperature"])
        self.assertEqual(self.device.template_id, self.starter.pk)
        self.assertEqual(publish_verified_submission(submission=submission, reviewer=self.user).pk, published.pk)

    def test_orphaned_customer_map_never_becomes_global(self):
        self._private_starter()
        self.starter.created_by_team = None
        self.starter.save(update_fields=["created_by_team"])
        self.assertFalse(visible_templates_for_team(self.team).filter(pk=self.starter.pk).exists())

    def test_deleting_team_removes_private_curated_starter_instead_of_exposing_it(self):
        owner = Team.objects.create(name="Demo owner", slug="demo-owner")
        private = DeviceTemplate.objects.create(
            name="Demo starter",
            created_by_team=owner,
            device_type="plc",
            protocol="modbus_tcp",
            mapping_strategy="site_defined",
            register_map={},
        )
        owner.delete()
        self.assertFalse(DeviceTemplate.objects.filter(pk=private.pk).exists())

    def test_share_action_is_explicit_and_keeps_mapping_private(self):
        self._private_starter()
        self._save_and_confirm()
        Membership.objects.create(team=self.team, user=self.user, role=ROLE_ADMIN)
        self.client.force_login(self.user)
        url = reverse("web_team:devices:device_datapoint_mapping", args=[self.team.slug, self.device.pk])
        page = self.client.get(url)
        self.assertContains(page, "Share this mapping with Novena")
        self.client.post(url, {"action": "share_with_novena"})
        self.assertFalse(TemplateShareSubmission.objects.exists())
        response = self.client.post(
            url,
            {
                "action": "share_with_novena",
                "share_consent": "on",
                "source_document_url": "https://manufacturer.example/manual",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TemplateShareSubmission.objects.count(), 1)
        page = self.client.get(url)
        self.assertContains(page, "already been submitted")

    def test_unconfirmed_site_mapping_is_excluded_from_connector_config(self):
        save_device_datapoint_map(device=self.device, team=self.team, datapoints=self.datapoints)

        self.assertEqual(generate_connector_config(self.gateway), [])
        self.assertEqual(effective_register_map(self.device), {})

    def test_confirmed_mapping_generates_edge_config_with_semantic_conversion(self):
        self._save_and_confirm()

        connectors = generate_connector_config(self.gateway)
        point = connectors[0]["config"]["master"]["slaves"][0]["timeseries"][0]

        self.assertEqual(point["tag"], "process_temperature")
        self.assertEqual(point["functionCode"], 3)
        self.assertEqual(point["type"], "32float")
        self.assertEqual(point["objectsCount"], 2)
        self.assertEqual(point["multiplier"], 0.1)
        self.assertEqual(point["offset"], -5.0)

    def test_fixed_function_template_keeps_existing_config_path(self):
        fixed = DeviceTemplate.objects.create(
            name="Fixed Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            mapping_strategy="fixed",
            register_map={"active_power": {"address": 12, "type": "float32", "unit": "kW"}},
        )
        Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            template=fixed,
            name="Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            connection_config={"host": "192.168.1.60", "port": 502},
        )

        connectors = generate_connector_config(self.gateway)
        slaves = connectors[0]["config"]["master"]["slaves"]

        self.assertEqual([slave["deviceName"] for slave in slaves], ["Meter"])
        self.assertEqual(slaves[0]["timeseries"][0]["tag"], "active_power")

    def test_legacy_writable_function_code_is_split_into_read_and_write_intent(self):
        datapoints = register_map_to_datapoints(
            {
                "digital_output": {
                    "address": 10,
                    "type": "bool",
                    "functionCode": 5,
                    "writable": True,
                },
                "speed_setpoint": {
                    "address": 20,
                    "type": "uint16",
                    "functionCode": 6,
                    "writable": True,
                },
            }
        )

        self.assertEqual(datapoints[0]["read_function_code"], 1)
        self.assertEqual(datapoints[0]["write_function_code"], 5)
        self.assertEqual(datapoints[1]["read_function_code"], 3)
        self.assertEqual(datapoints[1]["write_function_code"], 6)

    def test_stale_validation_cannot_confirm_after_connection_change(self):
        mapping = save_device_datapoint_map(device=self.device, team=self.team, datapoints=self.datapoints)
        checksum = mapping_checksum(self.device, mapping.datapoints)
        mapping = record_device_datapoint_validation(
            device=self.device,
            result={
                "status": "success",
                "mapping_checksum": checksum,
                "signals": [{"key": "process_temperature", "status": "success"}],
            },
        )
        self.device.connection_config["slave_id"] = 2
        self.device.save(update_fields=["connection_config"])

        with self.assertRaisesRegex(ValidationError, "changed after the live test"):
            confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)

    def test_failed_live_read_cannot_be_confirmed(self):
        mapping = save_device_datapoint_map(device=self.device, team=self.team, datapoints=self.datapoints)
        mapping = record_device_datapoint_validation(
            device=self.device,
            result={
                "status": "failed",
                "mapping_checksum": mapping_checksum(self.device, mapping.datapoints),
                "signals": [
                    {
                        "key": "process_temperature",
                        "status": "failed",
                        "reason": "Decoded value 9000 °C is outside the safety range.",
                    }
                ],
            },
        )

        health = mapping.datapoint_health["process_temperature"]
        self.assertEqual(health["status"], "failed")
        self.assertIn("outside the safety range", health["error_message"])
        with self.assertRaisesRegex(ValidationError, "successful live validation"):
            confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)

    def test_mapping_limit_matches_single_gateway_validation_batch(self):
        datapoints = [
            {
                "key": f"signal_{index}",
                "address": index,
                "data_type": "uint16",
            }
            for index in range(21)
        ]

        with self.assertRaisesRegex(ValidationError, "at most 20 signals"):
            save_device_datapoint_map(device=self.device, team=self.team, datapoints=datapoints)

    def test_confirmed_display_type_generates_requested_widget(self):
        self._save_and_confirm()

        dashboard = generate_default_dashboard(self.device)
        widget = dashboard.widgets.get(telemetry_key="process_temperature")

        self.assertEqual(widget.widget_type, "timeseries")
        self.assertEqual(widget.unit, "°C")

    def test_clone_is_team_scoped_and_requires_revalidation(self):
        source = self._save_and_confirm()
        target = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            template=self.starter,
            name="Second PLC",
            device_type="plc",
            protocol="modbus_tcp",
            connection_config={"host": "192.168.1.51", "port": 502},
        )

        cloned = clone_device_datapoint_map(source_device=self.device, target_device=target, team=self.team)

        self.assertEqual(cloned.status, DeviceDatapointMap.Status.DRAFT)
        self.assertEqual(cloned.cloned_from, source)
        self.assertFalse(cloned.tested_checksum)
        self.assertFalse(cloned.confirmed_checksum)

        other_team = Team.objects.create(name="Other", slug="other")
        with self.assertRaisesRegex(ValidationError, "current team"):
            clone_device_datapoint_map(source_device=self.device, target_device=target, team=other_team)

    def test_csv_round_trip_and_row_errors(self):
        exported = datapoints_to_csv(self.datapoints)

        imported = datapoints_from_csv(exported)

        self.assertEqual(imported[0]["key"], "process_temperature")
        self.assertEqual(imported[0]["objects_count"], 2)
        self.assertEqual(imported[0]["multiplier"], 0.1)
        self.assertEqual(imported[0]["safety_max"], 200.0)

        invalid = "key,address,function_code\nvalid_signal,1,3\nbad_address,nope,3\nvalid_signal,2,3\n"
        with self.assertRaises(ValidationError) as error:
            datapoints_from_csv(invalid)

        self.assertTrue(any("Row 3" in message for message in error.exception.messages))
        self.assertTrue(
            any("Row 4" in message and "duplicates row 2" in message for message in error.exception.messages)
        )

    def test_validation_audit_and_per_datapoint_warning_health(self):
        mapping = save_device_datapoint_map(device=self.device, team=self.team, datapoints=self.datapoints)
        checksum = mapping_checksum(self.device, mapping.datapoints)

        mapping = record_device_datapoint_validation(
            device=self.device,
            validated_by=self.user,
            result={
                "status": "success",
                "mapping_checksum": checksum,
                "signals": [
                    {
                        "key": "process_temperature",
                        "status": "warning",
                        "raw_value": [16830, 0],
                        "decoded_value": 85.0,
                        "warning_message": "Above the configured normal maximum.",
                        "blocking": False,
                    }
                ],
            },
        )

        self.assertEqual(mapping.status, DeviceDatapointMap.Status.AWAITING_CONFIRMATION)
        self.assertEqual(mapping.validated_by, self.user)
        self.assertIsNotNone(mapping.validated_at)
        health = mapping.datapoint_health["process_temperature"]
        self.assertEqual(health["status"], "warning")
        self.assertEqual(health["raw_value"], [16830, 0])
        self.assertEqual(health["decoded_value"], 85.0)
        self.assertTrue(health["warning_message"])
        self.assertTrue(health["validated_at"])

        confirmed = confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)
        revision = confirmed.revisions.get()
        self.assertEqual(confirmed.confirmed_by, self.user)
        self.assertEqual(revision.validated_by, self.user)
        self.assertEqual(revision.confirmed_by, self.user)

    def test_material_confirmations_create_revisions_and_rollback_requires_revalidation(self):
        self._save_and_confirm()
        first = DeviceDatapointMapRevision.objects.get(mapping__device=self.device, revision_number=1)
        changed = [dict(self.datapoints[0], address=110)]
        mapping = save_device_datapoint_map(device=self.device, team=self.team, datapoints=changed)
        record_device_datapoint_validation(
            device=self.device,
            validated_by=self.user,
            result={
                "status": "success",
                "mapping_checksum": mapping_checksum(self.device, mapping.datapoints),
                "signals": [{"key": "process_temperature", "status": "success", "decoded_value": 25.0}],
            },
        )
        confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)

        self.assertEqual(DeviceDatapointMapRevision.objects.filter(mapping__device=self.device).count(), 2)
        restored = rollback_device_datapoint_map(
            device=self.device,
            team=self.team,
            revision=first,
        )
        self.assertEqual(restored.status, DeviceDatapointMap.Status.DRAFT)
        self.assertEqual(restored.datapoints[0]["address"], 100)
        self.assertEqual(restored.datapoint_health, {})
        self.assertFalse(restored.tested_checksum)
        self.assertFalse(restored.confirmed_checksum)
        with self.assertRaisesRegex(ValidationError, "successful live validation"):
            confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)

        restored = record_device_datapoint_validation(
            device=self.device,
            validated_by=self.user,
            result={
                "status": "success",
                "mapping_checksum": mapping_checksum(self.device, restored.datapoints),
                "signals": [{"key": "process_temperature", "status": "success", "decoded_value": 20.0}],
            },
        )
        confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)
        latest = DeviceDatapointMapRevision.objects.filter(mapping__device=self.device).first()
        self.assertEqual(DeviceDatapointMapRevision.objects.filter(mapping__device=self.device).count(), 3)
        self.assertEqual(latest.datapoints[0]["address"], 100)

        other_team = Team.objects.create(name="Revision Attacker", slug="revision-attacker")
        with self.assertRaisesRegex(ValidationError, "current team"):
            rollback_device_datapoint_map(device=self.device, team=other_team, revision=first)

    def test_reconfirming_latest_revision_does_not_create_duplicate(self):
        self._save_and_confirm()
        revision = DeviceDatapointMapRevision.objects.get(mapping__device=self.device)
        mapping = rollback_device_datapoint_map(device=self.device, team=self.team, revision=revision)
        record_device_datapoint_validation(
            device=self.device,
            validated_by=self.user,
            result={
                "status": "success",
                "mapping_checksum": mapping_checksum(self.device, mapping.datapoints),
                "signals": [{"key": "process_temperature", "status": "success", "decoded_value": 20.0}],
            },
        )
        confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)

        self.assertEqual(DeviceDatapointMapRevision.objects.filter(mapping__device=self.device).count(), 1)

    def test_repeating_confirmation_of_unchanged_map_is_idempotent(self):
        first = self._save_and_confirm()

        second = confirm_device_datapoint_map(device=self.device, team=self.team, confirmed_by=self.user)

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.confirmed_checksum, first.confirmed_checksum)
        self.assertEqual(DeviceDatapointMapRevision.objects.filter(mapping=first).count(), 1)
