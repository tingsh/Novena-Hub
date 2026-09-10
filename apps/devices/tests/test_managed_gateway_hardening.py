import base64
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.devices.activation import (
    acknowledge_gateway_activation,
    encrypt_activation_secret,
    provision_gateway_activation,
)
from apps.devices.config_generator import generate_connector_config
from apps.devices.freshness import expected_device_interval_seconds
from apps.devices.gateway_config_delivery import (
    GatewayConfigUnsupported,
    acknowledge_gateway_config,
    dispatch_gateway_config_outbox,
    queue_gateway_config,
)
from apps.devices.gateway_release import dispatch_gateway_release, request_gateway_release
from apps.devices.models import (
    Device,
    DeviceTemplate,
    Gateway,
    GatewayActivation,
    GatewayConfig,
    GatewayInventory,
    GatewayPlanReconciliation,
    RemoteCommand,
    RpcCommand,
    Site,
)
from apps.devices.plan_reconciliation import (
    dispatch_due_plan_reconciliations,
    queue_team_plan_reconciliation,
    reconcile_team_gateway_polling,
)
from apps.teams.models import Team
from apps.telemetry.management.commands.mqtt_consumer import Command as MqttConsumerCommand
from apps.telemetry.models import GatewayLog, TelemetryData

SIGNING_SEED = base64.b64encode(b"1" * 32).decode()


class ManagedGatewayFixture(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Managed Team", slug="managed-team")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Managed Gateway",
            serial_number="NF-HARDEN-001",
            access_token="managed-token",
            mqtt_username="NF-HARDEN-001",
            mqtt_password="old-hash",
            gateway_capabilities=["guided_setup_v1"],
            lifecycle_status="active",
        )
        self.inventory = GatewayInventory.objects.create(
            serial_number=self.gateway.serial_number,
            status="claimed",
            gateway=self.gateway,
            claimed_by_team=self.team,
            claimed_at=timezone.now(),
        )


@override_settings(GATEWAY_ACTIVATION_ENCRYPTION_KEY="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
class GatewayReleaseHardeningTest(ManagedGatewayFixture):
    def _device(self):
        return Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
        )

    @patch("apps.devices.mqtt_provisioning.deprovision_gateway_mqtt", side_effect=RuntimeError("broker down"))
    def test_unknown_revocation_keeps_inventory_and_data_quarantined(self, _deprovision):
        device = self._device()
        release = request_gateway_release(self.gateway)

        dispatch_gateway_release(release.pk)

        release.refresh_from_db()
        self.gateway.refresh_from_db()
        self.inventory.refresh_from_db()
        self.assertEqual(release.status, "retry")
        self.assertEqual(self.gateway.lifecycle_status, "release_pending")
        self.assertEqual(self.inventory.status, "claimed")
        self.assertEqual(self.inventory.gateway, self.gateway)
        self.assertTrue(Device.objects.filter(pk=device.pk).exists())

    @patch("apps.devices.mqtt_provisioning.deprovision_gateway_mqtt")
    def test_verified_revocation_purges_operational_data_but_preserves_command_evidence(self, _deprovision):
        device = self._device()
        TelemetryData.objects.create(
            device=device,
            timestamp=timezone.now(),
            key="power",
            value_numeric=10,
        )
        GatewayLog.objects.create(
            gateway=self.gateway,
            timestamp=timezone.now(),
            level="INFO",
            logger_name="test",
            message="operational log",
        )
        RpcCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            request_id=uuid.uuid4(),
            method="ping",
        )
        activation = GatewayActivation.objects.create(
            team=self.team,
            gateway=self.gateway,
            generation=1,
            status="delivered",
            expires_at=timezone.now() + timedelta(hours=1),
            encrypted_mqtt_password=encrypt_activation_secret("plaintext-secret"),
        )
        command = RemoteCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            device=device,
            operation="read_device",
            risk=RemoteCommand.Risk.DIAGNOSTIC,
            status=RemoteCommand.Status.QUEUED,
            target_snapshot={
                "gateway_serial": self.gateway.serial_number,
                "device_id": device.pk,
                "device_name": device.name,
            },
            expires_at=timezone.now() + timedelta(minutes=1),
        )

        release = request_gateway_release(self.gateway)
        dispatch_gateway_release(release.pk)

        release.refresh_from_db()
        self.gateway.refresh_from_db()
        self.inventory.refresh_from_db()
        activation.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(release.status, "completed")
        self.assertEqual(self.gateway.lifecycle_status, "released")
        self.assertIsNone(self.gateway.mqtt_username)
        self.assertEqual(self.gateway.mqtt_password, "")
        self.assertEqual(self.inventory.status, "released")
        self.assertIsNone(self.inventory.gateway)
        self.assertFalse(Device.objects.filter(pk=device.pk).exists())
        self.assertFalse(TelemetryData.objects.exists())
        self.assertFalse(GatewayLog.objects.filter(gateway=self.gateway).exists())
        self.assertFalse(RpcCommand.objects.filter(gateway=self.gateway).exists())
        self.assertEqual(activation.encrypted_mqtt_password, "")
        self.assertIsNone(command.device)
        self.assertEqual(command.status, RemoteCommand.Status.CANCELLED)
        self.assertEqual(command.target_snapshot["device_name"], "Meter")

    def test_concurrent_release_requests_share_one_workflow(self):
        first = request_gateway_release(self.gateway)
        second = request_gateway_release(self.gateway)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.gateway.release_requests.count(), 1)


@override_settings(GATEWAY_ACTIVATION_ENCRYPTION_KEY="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
class GatewayActivationRecoveryTest(ManagedGatewayFixture):
    @override_settings(MQTT_PROVISIONING_REQUIRED=True)
    @patch("apps.telemetry.mqtt_publisher.publish_gateway_activation")
    @patch("apps.devices.mqtt_provisioning.provision_gateway_mqtt")
    def test_expired_activation_is_reissued_once_on_bootstrap_hello(self, provision, publish):
        expired = GatewayActivation.objects.create(
            team=self.team,
            gateway=self.gateway,
            generation=1,
            status="expired",
            expires_at=timezone.now() - timedelta(minutes=1),
            encrypted_mqtt_password="",
        )
        GatewayActivation.objects.filter(pk=expired.pk).update(created_at=timezone.now() - timedelta(hours=25))

        consumer = MqttConsumerCommand()
        consumer._handle_bootstrap_hello({"serial_number": self.gateway.serial_number})
        activation = self.gateway.activations.order_by("-generation").first()
        consumer._handle_bootstrap_hello({"serial_number": self.gateway.serial_number})

        self.assertEqual(activation.generation, 2)
        self.assertEqual(self.gateway.activations.count(), 2)
        self.assertEqual(activation.status, "provisioning")
        provision_gateway_activation(activation.pk)
        activation.refresh_from_db()
        self.assertEqual(activation.status, "delivered")
        provision.assert_called_once()
        publish.assert_called_once()

    def test_stale_generation_cannot_acknowledge_new_activation(self):
        activation = GatewayActivation.objects.create(
            team=self.team,
            gateway=self.gateway,
            generation=2,
            status="delivered",
            expires_at=timezone.now() + timedelta(hours=1),
            encrypted_mqtt_password=encrypt_activation_secret("secret"),
        )

        result = acknowledge_gateway_activation(
            self.gateway,
            str(activation.request_id),
            1,
            "success",
        )

        activation.refresh_from_db()
        self.assertIsNone(result)
        self.assertEqual(activation.status, "delivered")
        self.assertNotEqual(activation.encrypted_mqtt_password, "")

    def test_exact_acknowledgement_for_superseded_generation_is_ignored(self):
        old = GatewayActivation.objects.create(
            team=self.team,
            gateway=self.gateway,
            generation=1,
            status="superseded",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        current = GatewayActivation.objects.create(
            team=self.team,
            gateway=self.gateway,
            generation=2,
            status="delivered",
            expires_at=timezone.now() + timedelta(hours=1),
            encrypted_mqtt_password=encrypt_activation_secret("current-secret"),
        )

        result = acknowledge_gateway_activation(
            self.gateway,
            str(old.request_id),
            old.generation,
            "success",
        )

        old.refresh_from_db()
        current.refresh_from_db()
        self.assertIsNone(result)
        self.assertEqual(old.status, "superseded")
        self.assertEqual(current.status, "delivered")


@override_settings(
    REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID="config-test",
    REMOTE_CONTROL_SIGNING_KEYS={"config-test": SIGNING_SEED},
    REMOTE_CONTROL_SIGNING_PRIVATE_KEY=SIGNING_SEED,
)
class SignedConfigDeliveryHardeningTest(ManagedGatewayFixture):
    def setUp(self):
        super().setUp()
        self.gateway.status = "online"
        self.gateway.mqtt_connected = True
        self.gateway.last_seen = timezone.now()
        self.gateway.save(update_fields=["status", "mqtt_connected", "last_seen"])

    def _ack(self, config, status):
        return acknowledge_gateway_config(
            self.gateway,
            {
                "config_update_request_id": str(config.request_id),
                "config_revision": config.revision,
                "config_checksum": config.checksum,
                "config_idempotency_key": str(config.idempotency_key),
                "config_update_status": status,
            },
        )

    @patch("apps.telemetry.mqtt_publisher.publish_config_envelope")
    def test_puback_acceptance_and_terminal_ack_are_distinct(self, publish):
        config = queue_gateway_config(self.gateway, "connector_update", {"connectors": []})
        dispatch_gateway_config_outbox(config.outbox.pk)

        config.refresh_from_db()
        self.assertEqual(config.status, "published")
        self.assertIsNone(config.accepted_at)
        self._ack(config, "accepted")
        config.refresh_from_db()
        self.assertEqual(config.status, "accepted")
        self.assertIsNotNone(config.accepted_at)
        self._ack(config, "active")
        self._ack(config, "accepted")
        config.refresh_from_db()
        self.assertEqual(config.status, "active")
        self.assertIsNotNone(config.acknowledged_at)
        publish.assert_called_once()

    def test_missing_or_mismatched_ack_identity_is_rejected(self):
        config = queue_gateway_config(self.gateway, "connector_update", {"connectors": []})

        with self.assertRaisesMessage(ValueError, "missing identity"):
            acknowledge_gateway_config(
                self.gateway,
                {"config_update_request_id": str(config.request_id), "config_update_status": "active"},
            )
        with self.assertRaisesMessage(ValueError, "does not match"):
            acknowledge_gateway_config(
                self.gateway,
                {
                    "config_update_request_id": str(config.request_id),
                    "config_revision": config.revision + 1,
                    "config_checksum": config.checksum,
                    "config_idempotency_key": str(config.idempotency_key),
                    "config_update_status": "active",
                },
            )

    @patch("apps.telemetry.mqtt_publisher.publish_config_envelope")
    def test_offline_intent_waits_without_consuming_attempt_then_publishes(self, publish):
        self.gateway.last_seen = timezone.now() - timedelta(hours=1)
        self.gateway.save(update_fields=["last_seen"])
        config = queue_gateway_config(self.gateway, "connector_update", {"connectors": []})

        dispatch_gateway_config_outbox(config.outbox.pk)
        config.refresh_from_db()
        config.outbox.refresh_from_db()
        self.assertEqual(config.status, "waiting_for_gateway")
        self.assertEqual(config.outbox.attempt_count, 0)
        publish.assert_not_called()

        self.gateway.last_seen = timezone.now()
        self.gateway.save(update_fields=["last_seen"])
        config.outbox.next_attempt_at = timezone.now()
        config.outbox.save(update_fields=["next_attempt_at"])
        dispatch_gateway_config_outbox(config.outbox.pk)
        config.refresh_from_db()
        self.assertEqual(config.status, "published")
        publish.assert_called_once()

    def test_only_latest_timed_out_revision_can_recover_from_late_terminal_ack(self):
        first = queue_gateway_config(self.gateway, "connector_update", {"connectors": []})
        GatewayConfig.objects.filter(pk=first.pk).update(status="timed_out")
        self._ack(first, "active")
        first.refresh_from_db()
        self.assertEqual(first.status, "active")

        second = queue_gateway_config(
            self.gateway,
            "connector_update",
            {"connectors": [{"name": "new", "type": "modbus"}]},
        )
        self._ack(first, "active")
        first.refresh_from_db()
        self.assertEqual(first.status, "superseded")
        self.assertEqual(second.revision, 2)

    def test_unsupported_gateway_never_queues_unsigned_config(self):
        self.gateway.gateway_capabilities = []
        self.gateway.save(update_fields=["gateway_capabilities"])

        with self.assertRaises(GatewayConfigUnsupported):
            queue_gateway_config(self.gateway, "connector_update", {"connectors": []})

    def test_unsupported_connector_shape_is_rejected_before_queueing(self):
        with self.assertRaisesMessage(ValueError, "connector type"):
            queue_gateway_config(
                self.gateway,
                "connector_update",
                {"connectors": [{"name": "Missing type"}]},
            )
        self.assertFalse(GatewayConfig.objects.filter(gateway=self.gateway).exists())
        self.assertFalse(GatewayConfig.objects.exists())


@override_settings(
    REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID="plan-test",
    REMOTE_CONTROL_SIGNING_KEYS={"plan-test": SIGNING_SEED},
    REMOTE_CONTROL_SIGNING_PRIVATE_KEY=SIGNING_SEED,
)
class PlanPollingHardeningTest(ManagedGatewayFixture):
    def setUp(self):
        super().setUp()
        self.template = DeviceTemplate.objects.create(
            name="Meter Template",
            device_type="power_meter",
            protocol="modbus_tcp",
            default_polling_interval=2,
            register_map={"power": {"address": 1, "functionCode": 3}},
        )
        self.device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            template=self.template,
            name="Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
        )

    def test_plan_minimum_clamps_poll_period_without_speeding_slower_equipment(self):
        with patch("apps.subscriptions.enforcement.get_latency_limit_for_team", return_value=10.0):
            starter = generate_connector_config(self.gateway)[0]["config"]["master"]["slaves"][0]
        with patch("apps.subscriptions.enforcement.get_latency_limit_for_team", return_value=5.0):
            business = generate_connector_config(self.gateway)[0]["config"]["master"]["slaves"][0]
        with patch("apps.subscriptions.enforcement.get_latency_limit_for_team", return_value=1.0):
            enterprise = generate_connector_config(self.gateway)[0]["config"]["master"]["slaves"][0]
        self.template.default_polling_interval = 30
        self.template.save(update_fields=["default_polling_interval"])
        with patch("apps.subscriptions.enforcement.get_latency_limit_for_team", return_value=1.0):
            slow_equipment = generate_connector_config(self.gateway)[0]["config"]["master"]["slaves"][0]

        self.assertEqual(starter["pollPeriod"], 10000)
        self.assertEqual(business["pollPeriod"], 5000)
        self.assertEqual(enterprise["pollPeriod"], 2000)
        self.assertEqual(slow_equipment["pollPeriod"], 30000)

    def test_freshness_never_rounds_below_fractional_plan_minimum(self):
        with patch("apps.subscriptions.enforcement.get_latency_limit_for_team", return_value=2.5):
            self.assertEqual(expected_device_interval_seconds(self.device), 3)

    @patch("apps.devices.plan_reconciliation._schedule_reconciliation")
    def test_plan_reconciliation_is_idempotent_and_marks_unsupported_gateways(self, schedule):
        unsupported = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Old Gateway",
            serial_number="NF-HARDEN-OLD",
            access_token="old-gateway-token",
            lifecycle_status="active",
        )
        Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=unsupported,
            template=self.template,
            name="Old Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
        )
        first = queue_team_plan_reconciliation(self.team, 10, 5, "stripe:event-1")
        second = queue_team_plan_reconciliation(self.team, 10, 5, "stripe:event-1")

        result = reconcile_team_gateway_polling(first.pk)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(GatewayPlanReconciliation.objects.count(), 1)
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(result.queued_gateway_count, 1)
        self.assertEqual(result.unsupported_gateway_count, 1)
        self.assertIn(unsupported.serial_number, result.last_error)
        self.assertEqual(GatewayConfig.objects.filter(gateway=self.gateway).count(), 1)
        schedule.assert_not_called()

    @patch("apps.devices.plan_reconciliation.reconcile_team_gateway_polling")
    def test_plan_reconciliation_scanner_recovers_queued_work(self, reconcile):
        reconciliation = GatewayPlanReconciliation.objects.create(
            team=self.team,
            source_key="stripe:queued-event",
            previous_interval_seconds=10,
            new_interval_seconds=5,
        )

        count = dispatch_due_plan_reconciliations()

        self.assertEqual(count, 1)
        reconcile.assert_called_once_with(reconciliation.pk)
