import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    transaction,
)
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.devices.models import CommandEvent, CommandOutbox, Gateway, RemoteCommand, Site
from apps.devices.remote_control import (
    CommandDenied,
    append_command_event,
    dispatch_due_outboxes,
    dispatch_outbox,
    request_remote_command,
    transition_command,
)
from apps.devices.remote_control_crypto import build_signed_command_envelope
from apps.devices.remote_control_protocol import canonical_device_operation
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser

CAPABILITIES = [
    "governed_commands_v1",
    "local_writeback_v1",
    "lifecycle_stages_v1",
    "idempotent_replay_v1",
]


class RemoteControlP1Test(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create(email="owner@example.com", username="owner")
        self.team = Team.objects.create(
            name="P1",
            slug="p1",
            remote_control_mode=Team.RemoteControlMode.CONTROLLED,
        )
        Membership.objects.create(team=self.team, user=self.user, role="owner")
        self.site = Site.objects.create(team=self.team, name="Plant")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="GW",
            serial_number="GW-P1-001",
            access_token="p1-token",
            remote_control_protocol_version=1,
            remote_control_capabilities=CAPABILITIES,
            remote_control_local_writeback_enabled=True,
        )

    def _queued_command(self, *, operation="ping"):
        command = RemoteCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            requested_by=self.user,
            operation=operation,
            risk=RemoteCommand.Risk.DIAGNOSTIC,
            status=RemoteCommand.Status.QUEUED,
            request_payload={"method": operation, "params": {}},
            control_epoch=self.team.remote_control_epoch,
            expires_at=timezone.now() + timedelta(minutes=1),
        )
        outbox = CommandOutbox.objects.create(command=command)
        append_command_event(
            command,
            "command_queued",
            from_status=RemoteCommand.Status.REQUESTED,
            to_status=RemoteCommand.Status.QUEUED,
        )
        return command, outbox

    def test_old_gateway_remains_monitoring_compatible_but_command_ineligible(self):
        self.gateway.remote_control_protocol_version = 0
        self.gateway.remote_control_capabilities = []
        self.gateway.remote_control_local_writeback_enabled = False
        self.gateway.save(
            update_fields=[
                "remote_control_protocol_version",
                "remote_control_capabilities",
                "remote_control_local_writeback_enabled",
            ]
        )
        diagnostic = request_remote_command(
            gateway=self.gateway,
            operation="ping",
            requested_by=self.user,
        )
        self.assertEqual(diagnostic.status, RemoteCommand.Status.QUEUED)

        with self.assertRaises(CommandDenied) as raised:
            request_remote_command(
                gateway=self.gateway,
                operation="update_firmware",
                requested_by=self.user,
                params={"manifest": {}, "signature": ""},
            )
        self.assertEqual(raised.exception.code, "protocol_not_advertised")

    def test_signed_setup_diagnostic_is_blocked_until_gateway_clock_is_ready(self):
        self.gateway.remote_control_clock_ready = False
        self.gateway.save(update_fields=["remote_control_clock_ready"])

        with self.assertRaises(CommandDenied) as raised:
            request_remote_command(
                gateway=self.gateway,
                operation="deployment_discover",
                requested_by=self.user,
                params={"scope": "attached_interfaces"},
            )

        self.assertEqual(raised.exception.code, "gateway_clock_not_ready")
        command = RemoteCommand.objects.get(operation="deployment_discover")
        self.assertEqual(command.status, RemoteCommand.Status.POLICY_DENIED)

    def test_unknown_compatibility_methods_are_rejected_without_write_inference(self):
        with self.assertRaisesRegex(ValueError, "Unknown or unsupported"):
            canonical_device_operation("toggle")
        with self.assertRaisesRegex(ValueError, "Unknown or unsupported"):
            canonical_device_operation("")

    def test_mode_disable_between_request_and_dispatch_cancels_without_publish(self):
        command, outbox = self._queued_command(operation="update_firmware")
        self.team.remote_control_mode = Team.RemoteControlMode.MONITORING_ONLY
        self.team.save(update_fields=["remote_control_mode"])
        with patch("apps.telemetry.mqtt_publisher.publish_rpc_command") as publish:
            dispatch_outbox(outbox.pk)
        command.refresh_from_db()
        outbox.refresh_from_db()
        self.assertEqual(command.status, RemoteCommand.Status.CANCELLED)
        self.assertEqual(command.error_code, "remote_control_revoked")
        self.assertEqual(outbox.status, CommandOutbox.Status.CANCELLED)
        publish.assert_not_called()

    def test_epoch_change_between_request_and_dispatch_cancels_without_publish(self):
        command, outbox = self._queued_command(operation="update_firmware")
        self.team.remote_control_epoch += 1
        self.team.save(update_fields=["remote_control_epoch"])
        with patch("apps.telemetry.mqtt_publisher.publish_rpc_command") as publish:
            dispatch_outbox(outbox.pk)
        command.refresh_from_db()
        self.assertEqual(command.status, RemoteCommand.Status.CANCELLED)
        self.assertEqual(command.error_code, "control_epoch_changed")
        publish.assert_not_called()

    @patch(
        "apps.devices.remote_control_crypto.build_signed_command_envelope",
        return_value={"signing_key_id": "test", "signature": "sig"},
    )
    @patch("apps.telemetry.mqtt_publisher.publish_rpc_command")
    def test_periodic_scanner_recovers_committed_row_and_duplicate_is_idempotent(self, publish, _sign):
        command, outbox = self._queued_command()
        publish.return_value = SimpleNamespace(request_id=uuid.uuid4())
        self.assertEqual(dispatch_due_outboxes(), 1)
        self.assertEqual(dispatch_due_outboxes(), 0)
        outbox.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(outbox.status, CommandOutbox.Status.PUBLISHED)
        self.assertEqual(command.transport_status, "broker_acknowledged")
        publish.assert_called_once()

    @override_settings(
        REMOTE_CONTROL_OUTBOX_MAX_ATTEMPTS=2,
        REMOTE_CONTROL_OUTBOX_RETRY_BASE_SECONDS=1,
    )
    @patch(
        "apps.devices.remote_control_crypto.build_signed_command_envelope",
        return_value={"signing_key_id": "test", "signature": "sig"},
    )
    @patch("apps.telemetry.mqtt_publisher.publish_rpc_command", side_effect=RuntimeError("broker unavailable"))
    def test_pre_ack_failure_retries_then_dead_letters(self, _publish, _sign):
        command, outbox = self._queued_command()
        dispatch_outbox(outbox.pk)
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, CommandOutbox.Status.RETRY)
        self.assertEqual(outbox.attempt_count, 1)
        CommandOutbox.objects.filter(pk=outbox.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        dispatch_outbox(outbox.pk)
        outbox.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(outbox.status, CommandOutbox.Status.DEAD_LETTER)
        self.assertEqual(outbox.attempt_count, 2)
        self.assertEqual(command.status, RemoteCommand.Status.FAILED)

    @patch(
        "apps.devices.remote_control_crypto.build_signed_command_envelope",
        return_value={"signing_key_id": "test", "signature": "sig"},
    )
    @patch("apps.telemetry.mqtt_publisher.publish_rpc_command")
    def test_expired_worker_lease_is_recovered(self, publish, _sign):
        command, outbox = self._queued_command()
        CommandOutbox.objects.filter(pk=outbox.pk).update(
            status=CommandOutbox.Status.CLAIMED,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        publish.return_value = SimpleNamespace(request_id=uuid.uuid4())
        self.assertEqual(dispatch_due_outboxes(), 1)
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, CommandOutbox.Status.PUBLISHED)

    @override_settings(REMOTE_CONTROL_OUTBOX_MAX_ATTEMPTS=1)
    @patch("apps.telemetry.mqtt_publisher.publish_rpc_command")
    def test_repeated_worker_crash_dead_letters_expired_lease(self, publish):
        command, outbox = self._queued_command()
        CommandOutbox.objects.filter(pk=outbox.pk).update(
            status=CommandOutbox.Status.CLAIMED,
            attempt_count=1,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(dispatch_due_outboxes(), 1)
        outbox.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(outbox.status, CommandOutbox.Status.DEAD_LETTER)
        self.assertEqual(command.error_code, "dispatch_attempts_exhausted")
        publish.assert_not_called()

    @patch(
        "apps.devices.remote_control_crypto.build_signed_command_envelope",
        return_value={"signing_key_id": "test", "signature": "sig"},
    )
    @patch("apps.telemetry.mqtt_publisher.publish_rpc_command")
    def test_ambiguous_publish_is_not_blindly_retried(self, publish, _sign):
        from apps.telemetry.mqtt_publisher import MqttPublishOutcomeUnknown

        command, outbox = self._queued_command()
        rpc_record = SimpleNamespace(request_id=uuid.uuid4())
        publish.side_effect = MqttPublishOutcomeUnknown("ack unknown", rpc_record=rpc_record)
        dispatch_outbox(outbox.pk)
        self.assertIsNone(dispatch_outbox(outbox.pk))
        outbox.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(outbox.status, CommandOutbox.Status.PUBLISHED)
        self.assertEqual(command.status, RemoteCommand.Status.OUTCOME_UNKNOWN)
        self.assertEqual(command.transport_status, "outcome_unknown")
        publish.assert_called_once()

    @patch(
        "apps.devices.remote_control_crypto.build_signed_command_envelope",
        return_value={"signing_key_id": "test", "signature": "sig"},
    )
    @patch("apps.telemetry.mqtt_publisher.publish_rpc_command")
    def test_gateway_receipt_before_publish_update_never_regresses_status(self, publish, _sign):
        command, outbox = self._queued_command()

        def receive_first(*args, **kwargs):
            transition_command(
                command,
                RemoteCommand.Status.GATEWAY_RECEIVED,
                "gateway_received",
                updates={"execution_status": "gateway_received"},
            )
            return SimpleNamespace(request_id=uuid.uuid4())

        publish.side_effect = receive_first
        dispatch_outbox(outbox.pk)
        command.refresh_from_db()
        self.assertEqual(command.status, RemoteCommand.Status.GATEWAY_RECEIVED)
        self.assertEqual(command.transport_status, "broker_acknowledged")

    def test_events_are_sequenced_hash_chained_and_immutable(self):
        command, _ = self._queued_command()
        second = append_command_event(command, "second")
        events = list(command.events.order_by("sequence_number"))
        self.assertEqual([event.sequence_number for event in events], [1, 2])
        self.assertNotEqual(events[0].checksum, second.checksum)
        with self.assertRaises(TypeError):
            CommandEvent.objects.filter(pk=second.pk).update(event_type="tampered")
        with self.assertRaises(TypeError):
            second.delete()
        with self.assertRaises(ProtectedError):
            command.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommandEvent.objects.create(
                command=command,
                sequence_number=second.sequence_number,
                event_type="concurrent-duplicate",
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE devices_commandevent SET event_type = %s WHERE id = %s",
                ["tampered", second.pk.hex],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM devices_commandevent WHERE id = %s",
                [second.pk.hex],
            )


class GovernedEnvelopeContractTest(TestCase):
    def test_fixture_is_exact_hub_produced_payload(self):
        fixture = json.loads((Path(settings.BASE_DIR) / "tests/fixtures/governed_command_v1.json").read_text())
        command = SimpleNamespace(
            schema_version=1,
            pk=uuid.UUID("11111111-1111-4111-8111-111111111111"),
            idempotency_key=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            gateway=SimpleNamespace(serial_number="GW-CONTRACT-001"),
            device_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            operation="write_device",
            request_payload={"params": fixture["params"]},
            risk="high",
            control_epoch=7,
            sequence_number=42,
            template_revision=3,
            commissioning_revision=2,
            policy_revision=4,
            policy_checksum="contract-policy-v4",
            created_at=datetime(2026, 7, 25, tzinfo=UTC),
            expires_at=datetime(2026, 7, 25, 0, 0, 30, tzinfo=UTC),
        )
        with patch(
            "apps.devices.remote_control_crypto.sign_payload",
            return_value=("contract-key", "base64-ed25519-signature"),
        ):
            payload = build_signed_command_envelope(
                command,
                request_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
            )
        self.assertEqual(payload, fixture)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock concurrency test")
class CommandEventConcurrencyTest(TransactionTestCase):
    reset_sequences = True

    def _fixture_teardown(self):
        # TRUNCATE bypasses the intentional append-only DELETE trigger before Django flushes.
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE devices_commandevent RESTART IDENTITY CASCADE")
        super()._fixture_teardown()

    def test_parallel_appends_serialize_to_unique_sequences(self):
        team = Team.objects.create(name="Concurrency", slug="concurrency")
        site = Site.objects.create(team=team, name="Plant")
        gateway = Gateway.objects.create(
            team=team,
            site=site,
            name="GW",
            serial_number="GW-CONCURRENCY",
            access_token="token",
        )
        command = RemoteCommand.objects.create(
            team=team,
            gateway=gateway,
            operation="ping",
            risk=RemoteCommand.Risk.DIAGNOSTIC,
            status=RemoteCommand.Status.QUEUED,
            request_payload={"method": "ping", "params": {}},
            expires_at=timezone.now() + timedelta(minutes=1),
        )
        barrier = threading.Barrier(2)
        errors = []

        def append(event_type):
            close_old_connections()
            try:
                barrier.wait()
                append_command_event(command, event_type)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=append, args=("parallel-a",)),
            threading.Thread(target=append, args=("parallel-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(
            list(command.events.order_by("sequence_number").values_list("sequence_number", flat=True)),
            [1, 2],
        )
