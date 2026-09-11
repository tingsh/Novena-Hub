from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.test import TestCase, override_settings

from apps.devices.activation import encrypt_activation_secret
from apps.devices.config_generator import generate_connector_config
from apps.devices.models import Device, DeviceTemplate, Gateway, GatewayActivation, GatewayInventory, Site
from apps.devices.services import GatewayClaimError, claim_gateway_for_team, compute_claim_code
from apps.teams.models import Team


@override_settings(GATEWAY_ACTIVATION_ENCRYPTION_KEY="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
class GatewayClaimWorkflowTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Acme", slug="acme")
        self.other_team = Team.objects.create(name="Other", slug="other")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.serial = "NF-EDGE-001"
        self.claim_code = compute_claim_code(self.serial)
        self.inventory = GatewayInventory.objects.create(serial_number=self.serial)

    @override_settings(MQTT_PROVISIONING_REQUIRED=True)
    @patch("apps.telemetry.mqtt_publisher.publish_gateway_activation")
    @patch("apps.devices.mqtt_provisioning.provision_gateway_mqtt")
    def test_valid_inventory_claim_creates_gateway_and_marks_inventory_claimed_when_provisioning_required(
        self, mock_provision, mock_publish
    ):
        from apps.devices.activation import provision_gateway_activation

        gateway = claim_gateway_for_team(self.team, self.site, "Main Gateway", self.serial, self.claim_code)
        provision_gateway_activation(gateway.activations.get().pk)
        gateway.refresh_from_db()

        self.assertEqual(gateway.team, self.team)
        self.assertEqual(gateway.site, self.site)
        self.assertEqual(gateway.serial_number, self.serial)
        self.assertEqual(gateway.mqtt_username, self.serial)
        self.assertNotEqual(gateway.mqtt_password, self.claim_code)
        self.assertTrue(check_password(mock_provision.call_args.args[1], gateway.mqtt_password))
        self.assertEqual(gateway.lifecycle_status, "claimed")
        self.assertEqual(gateway.mqtt_provisioning_status, "success")
        activation = GatewayActivation.objects.get(gateway=gateway)
        self.assertEqual(activation.status, "pending")
        self.assertNotIn(mock_provision.call_args.args[1], activation.encrypted_mqtt_password)
        mock_publish.assert_not_called()

        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.status, "claimed")
        self.assertEqual(self.inventory.claimed_by_team, self.team)
        self.assertEqual(self.inventory.gateway, gateway)
        self.assertIsNotNone(self.inventory.claimed_at)
        mock_provision.assert_called_once()

    @override_settings(MQTT_PROVISIONING_REQUIRED=False)
    @patch("apps.telemetry.mqtt_publisher.publish_gateway_activation")
    @patch("apps.devices.mqtt_provisioning.provision_gateway_mqtt")
    def test_local_replay_claim_skips_dynsec_provisioning(self, mock_provision, mock_publish):
        from apps.devices.activation import decrypt_activation_secret, provision_gateway_activation

        gateway = claim_gateway_for_team(self.team, self.site, "Main Gateway", self.serial, self.claim_code)
        activation = gateway.activations.get()
        operational_password = decrypt_activation_secret(activation.encrypted_mqtt_password)

        provision_gateway_activation(activation.pk)
        gateway.refresh_from_db()
        activation.refresh_from_db()

        self.assertEqual(gateway.mqtt_username, self.serial)
        self.assertTrue(check_password(operational_password, gateway.mqtt_password))
        self.assertEqual(gateway.mqtt_provisioning_status, "success")
        self.assertEqual(gateway.mqtt_provisioning_error, "")
        self.assertEqual(activation.status, "pending")
        mock_provision.assert_not_called()
        mock_publish.assert_not_called()

    @override_settings(MQTT_PROVISIONING_REQUIRED=True)
    @patch("apps.devices.mqtt_provisioning.provision_gateway_mqtt", side_effect=RuntimeError("broker down"))
    def test_required_broker_provisioning_failure_keeps_durable_claim_for_retry(self, mock_provision):
        from apps.devices.activation import provision_gateway_activation

        gateway = claim_gateway_for_team(self.team, self.site, "Main Gateway", self.serial, self.claim_code)
        provision_gateway_activation(gateway.activations.get().pk)

        gateway.refresh_from_db()
        self.assertEqual(gateway.mqtt_provisioning_status, "failed")
        self.assertEqual(gateway.activations.get().status, "retry")
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.status, "claimed")

    def test_invalid_claim_code_fails(self):
        with self.assertRaises(GatewayClaimError):
            claim_gateway_for_team(self.team, self.site, "Main Gateway", self.serial, "BADCODE")

    def test_serial_must_exist_in_factory_inventory(self):
        serial = "NF-NOT-MADE"
        with self.assertRaises(GatewayClaimError):
            claim_gateway_for_team(self.team, self.site, "Unknown Gateway", serial, compute_claim_code(serial))

    @patch("apps.telemetry.mqtt_publisher.publish_gateway_activation")
    @patch("apps.devices.mqtt_provisioning.provision_gateway_mqtt")
    def test_claimed_gateway_cannot_move_to_another_team(self, mock_provision, mock_publish):
        gateway = claim_gateway_for_team(self.team, self.site, "Main Gateway", self.serial, self.claim_code)
        other_site = Site.objects.create(team=self.other_team, name="Other Site")

        with self.assertRaises(GatewayClaimError):
            claim_gateway_for_team(self.other_team, other_site, "Stolen Gateway", self.serial, self.claim_code)

        gateway.refresh_from_db()
        self.assertEqual(gateway.team, self.team)

    @override_settings(MQTT_PROVISIONING_REQUIRED=True)
    @patch("apps.telemetry.mqtt_publisher.publish_gateway_activation")
    @patch("apps.devices.mqtt_provisioning.provision_gateway_mqtt")
    def test_bootstrap_hello_retries_pending_activation(self, mock_provision, mock_publish):
        from apps.devices.activation import provision_gateway_activation
        from apps.telemetry.management.commands.mqtt_consumer import Command

        gateway = claim_gateway_for_team(self.team, self.site, "Main Gateway", self.serial, self.claim_code)
        provision_gateway_activation(gateway.activations.get().pk)
        activation = gateway.activations.get()

        Command()._handle_bootstrap_hello({"serial_number": self.serial})
        provision_gateway_activation(activation.pk)

        activation.refresh_from_db()
        gateway.refresh_from_db()
        self.assertEqual(activation.status, "delivered")
        self.assertEqual(activation.attempt_count, 1)
        self.assertIsNotNone(activation.delivered_at)
        self.assertEqual(gateway.lifecycle_status, "activating")
        mock_publish.assert_called_once()

    def test_activation_acknowledgement_clears_secret_and_is_idempotent(self):
        from django.utils import timezone

        from apps.telemetry.management.commands.mqtt_consumer import Command

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Ack Gateway",
            serial_number="NF-ACK-001",
            access_token="ack-token",
            mqtt_username="NF-ACK-001",
            lifecycle_status="activating",
        )
        activation = GatewayActivation.objects.create(
            team=self.team,
            gateway=gateway,
            status="delivered",
            expires_at=timezone.now() + timezone.timedelta(hours=1),
            encrypted_mqtt_password=encrypt_activation_secret("secret-password"),
        )
        GatewayInventory.objects.create(
            serial_number=gateway.serial_number,
            status="claimed",
            gateway=gateway,
            claimed_by_team=self.team,
        )

        payload = {
            "serial_number": gateway.serial_number,
            "attributes": {
                "credential_update_status": "success",
                "credential_update_action": "activate",
                "credential_update_request_id": str(activation.request_id),
                "credential_update_generation": activation.generation,
            },
        }
        consumer = Command()
        consumer._handle_attributes(payload)
        consumer._handle_attributes(payload)

        activation.refresh_from_db()
        gateway.refresh_from_db()
        self.assertEqual(activation.status, "acknowledged")
        self.assertEqual(activation.encrypted_mqtt_password, "")
        self.assertEqual(gateway.credential_rotation_status, "success")
        self.assertEqual(gateway.lifecycle_status, "online")

    def test_stale_activation_ack_does_not_acknowledge_current_activation(self):
        from uuid import uuid4

        from django.utils import timezone

        from apps.telemetry.management.commands.mqtt_consumer import Command

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Stale Ack Gateway",
            serial_number="NF-STALE-ACK",
            access_token="stale-ack-token",
            mqtt_username="NF-STALE-ACK",
            lifecycle_status="activating",
        )
        activation = GatewayActivation.objects.create(
            team=self.team,
            gateway=gateway,
            status="delivered",
            expires_at=timezone.now() + timezone.timedelta(hours=1),
            encrypted_mqtt_password=encrypt_activation_secret("secret-password"),
        )
        GatewayInventory.objects.create(
            serial_number=gateway.serial_number,
            status="claimed",
            gateway=gateway,
            claimed_by_team=self.team,
        )

        Command()._handle_attributes(
            {
                "serial_number": gateway.serial_number,
                "attributes": {
                    "credential_update_status": "success",
                    "credential_update_action": "activate",
                    "credential_update_request_id": str(uuid4()),
                    "credential_update_generation": activation.generation,
                },
            }
        )

        activation.refresh_from_db()
        self.assertEqual(activation.status, "delivered")
        self.assertNotEqual(activation.encrypted_mqtt_password, "")

    def test_expiry_task_clears_unacknowledged_activation_secret(self):
        from django.utils import timezone

        from apps.devices.tasks import expire_and_retry_gateway_activations

        gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Expired Gateway",
            serial_number="NF-EXPIRED-001",
            access_token="expired-token",
            mqtt_username="NF-EXPIRED-001",
        )
        activation = GatewayActivation.objects.create(
            team=self.team,
            gateway=gateway,
            status="pending",
            expires_at=timezone.now() - timezone.timedelta(minutes=1),
            encrypted_mqtt_password=encrypt_activation_secret("expired-password"),
        )

        result = expire_and_retry_gateway_activations()

        activation.refresh_from_db()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(activation.status, "expired")
        self.assertEqual(activation.encrypted_mqtt_password, "")


class GatewayMqttProvisioningAclTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="ACL Team", slug="acl-team")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="ACL Gateway",
            serial_number="NF-ACL-001",
            access_token="acl-token",
        )

    @patch("apps.devices.mqtt_provisioning._publish_dynsec_command")
    def test_operational_gateway_role_uses_only_serial_scoped_publish_acls(self, mock_publish):
        from apps.devices.mqtt_provisioning import provision_gateway_mqtt

        provision_gateway_mqtt(self.gateway, "operational-secret")

        commands = [call.args[0] for call in mock_publish.call_args_list]
        role_command = next(command for command in commands if command.get("rolename") == "gw-NF-ACL-001")
        send_topics = {acl["topic"] for acl in role_command["acls"] if acl["acltype"] == "publishClientSend"}
        self.assertEqual(
            send_topics,
            {
                "v1/gateway/NF-ACL-001/telemetry",
                "v1/gateway/NF-ACL-001/logs",
                "v1/gateway/NF-ACL-001/attributes",
                "v1/gateway/NF-ACL-001/rpc/response",
            },
        )
        self.assertNotIn("v1/gateway/telemetry", send_topics)

    def test_unknown_dynsec_command_is_never_treated_as_not_found(self):
        from apps.devices.mqtt_provisioning import _is_expected_error

        self.assertFalse(_is_expected_error("Unknown command", allow_not_found=True))
        self.assertTrue(_is_expected_error("Client not found", allow_not_found=True))


class EdgeConfigGenerationTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Acme", slug="acme")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Main Gateway",
            serial_number="NF-CFG-001",
            access_token="tok_cfg_001",
        )
        self.template = DeviceTemplate.objects.create(
            name="Schneider PM5350",
            manufacturer="Schneider",
            model_number="PM5350",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={
                "voltage": {"address": 3028, "type": "float32", "functionCode": 3},
                "active_power": {"address": 3054, "type": "float32", "functionCode": 3},
                "run_command": {"address": 1, "type": "bool", "functionCode": 5, "writable": True},
            },
        )

    def test_modbus_tcp_config_matches_edge_connector_contract(self):
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Power Meter 1",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_tcp",
            discovery_meta={"interface": "10.0.0.20:502", "slave_id": 3},
        )

        connectors = generate_connector_config(self.gateway)

        self.assertEqual(len(connectors), 1)
        connector = connectors[0]
        self.assertEqual(connector["type"], "modbus")
        self.assertIn("config", connector)
        slave = connector["config"]["master"]["slaves"][0]
        self.assertEqual(slave["host"], "10.0.0.20")
        self.assertEqual(slave["port"], 502)
        self.assertEqual(slave["unitId"], 3)
        self.assertEqual(slave["deviceId"], str(device.id))
        self.assertEqual(slave["type"], "tcp")
        tags = {entry["tag"]: entry for entry in slave["timeseries"]}
        self.assertEqual(tags["voltage"]["type"], "32float")
        self.assertEqual(tags["voltage"]["objectsCount"], 2)
        self.assertNotIn("run_command", tags)

    def test_modbus_rtu_config_keeps_serial_settings_in_slave(self):
        self.template.protocol = "modbus_rtu"
        self.template.save(update_fields=["protocol"])
        Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="RTU Meter",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_rtu",
            discovery_meta={"interface": "/dev/ttyUSB0", "baud_rate": 19200, "slave_id": 7},
        )

        connectors = generate_connector_config(self.gateway)
        slave = connectors[0]["config"]["master"]["slaves"][0]

        self.assertEqual(connectors[0]["type"], "modbus")
        self.assertEqual(slave["type"], "serial")
        self.assertEqual(slave["port"], "/dev/ttyUSB0")
        self.assertEqual(slave["baudrate"], 19200)
        self.assertEqual(slave["unitId"], 7)


@override_settings(GATEWAY_ACTIVATION_ENCRYPTION_KEY="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
class GatewayDeleteReleaseViewTest(TestCase):
    def setUp(self):
        from django.test import Client
        from django.utils import timezone

        from apps.teams.models import Membership
        from apps.teams.roles import ROLE_ADMIN, ROLE_VIEWER
        from apps.users.models import CustomUser

        self.client = Client()
        self.team = Team.objects.create(name="Acme", slug="acme")
        self.other_team = Team.objects.create(name="Other", slug="other")
        self.admin = CustomUser.objects.create(username="admin@example.com", email="admin@example.com")
        self.viewer = CustomUser.objects.create(username="viewer@example.com", email="viewer@example.com")
        Membership.objects.create(user=self.admin, team=self.team, role=ROLE_ADMIN)
        Membership.objects.create(user=self.viewer, team=self.team, role=ROLE_VIEWER)
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.other_site = Site.objects.create(team=self.other_team, name="Other Factory")
        self.serial = "NF-DELETE-001"
        self.claim_code = compute_claim_code(self.serial)
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Delete Me",
            serial_number=self.serial,
            access_token="delete-token-001",
            mqtt_username=self.serial,
            mqtt_password=self.claim_code,
        )
        self.inventory = GatewayInventory.objects.create(
            serial_number=self.serial,
            status="claimed",
            gateway=self.gateway,
            claimed_by_team=self.team,
            claimed_at=timezone.now(),
        )

    def _delete_url(self):
        from django.urls import reverse

        return reverse("web_team:devices:gateway_delete", args=[self.team.slug, self.gateway.pk])

    def test_admin_can_view_confirmation_page(self):
        self.client.force_login(self.admin)

        response = self.client.get(self._delete_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deleting this gateway will disconnect any devices connected through it.")
        self.assertContains(response, self.serial)

    def test_viewer_cannot_delete_gateway(self):
        self.client.force_login(self.viewer)

        response = self.client.post(self._delete_url(), {"confirmation_serial": self.serial})

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Gateway.objects.filter(pk=self.gateway.pk).exists())

    @patch("apps.devices.mqtt_provisioning.deprovision_gateway_mqtt")
    def test_missing_serial_confirmation_does_not_delete(self, mock_deprovision):
        self.client.force_login(self.admin)

        response = self.client.post(self._delete_url(), {"confirmation_serial": ""})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Type the gateway serial number exactly to confirm deletion.")
        self.assertTrue(Gateway.objects.filter(pk=self.gateway.pk).exists())
        mock_deprovision.assert_not_called()

    @patch("apps.devices.mqtt_provisioning.deprovision_gateway_mqtt")
    def test_wrong_serial_confirmation_does_not_delete(self, mock_deprovision):
        self.client.force_login(self.admin)

        response = self.client.post(self._delete_url(), {"confirmation_serial": "WRONG-SERIAL"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Type the gateway serial number exactly to confirm deletion.")
        self.assertTrue(Gateway.objects.filter(pk=self.gateway.pk).exists())
        mock_deprovision.assert_not_called()

    @patch("apps.devices.mqtt_provisioning.deprovision_gateway_mqtt")
    def test_matching_serial_releases_gateway_for_redo(self, mock_deprovision):
        from apps.devices.gateway_release import dispatch_gateway_release

        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Power Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
        )
        self.client.force_login(self.admin)

        response = self.client.post(self._delete_url(), {"confirmation_serial": self.serial.lower()})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Gateway.objects.filter(pk=self.gateway.pk).exists())
        self.assertTrue(Device.objects.filter(pk=device.pk).exists())
        mock_deprovision.assert_not_called()
        self.gateway.refresh_from_db()
        self.inventory.refresh_from_db()
        self.assertEqual(self.gateway.lifecycle_status, "release_pending")
        self.assertEqual(self.inventory.status, "claimed")

        dispatch_gateway_release(self.gateway.release_requests.get().pk)

        self.assertFalse(Device.objects.filter(pk=device.pk).exists())
        mock_deprovision.assert_called_once()
        self.assertEqual(mock_deprovision.call_args.args[0].serial_number, self.serial)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.status, "released")
        self.assertIsNone(self.inventory.gateway)
        self.assertIsNone(self.inventory.claimed_by_team)
        self.assertIsNone(self.inventory.claimed_at)
        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.lifecycle_status, "released")

    @override_settings(MQTT_PROVISIONING_REQUIRED=True)
    @patch("apps.telemetry.mqtt_publisher.publish_gateway_activation")
    @patch("apps.devices.mqtt_provisioning.provision_gateway_mqtt")
    @patch("apps.devices.mqtt_provisioning.deprovision_gateway_mqtt")
    def test_released_gateway_can_be_onboarded_by_another_team(self, mock_deprovision, mock_provision, mock_publish):
        from apps.devices.activation import provision_gateway_activation
        from apps.devices.gateway_release import dispatch_gateway_release

        self.client.force_login(self.admin)
        self.client.post(self._delete_url(), {"confirmation_serial": self.serial})
        dispatch_gateway_release(self.gateway.release_requests.get().pk)

        gateway = claim_gateway_for_team(
            self.other_team, self.other_site, "Reclaimed Gateway", self.serial, self.claim_code
        )
        provision_gateway_activation(gateway.activations.get().pk)

        self.assertEqual(gateway.team, self.other_team)
        self.assertEqual(gateway.site, self.other_site)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.status, "claimed")
        self.assertEqual(self.inventory.claimed_by_team, self.other_team)
        self.assertEqual(self.inventory.gateway, gateway)
        self.assertEqual(gateway.team, self.other_team)
        self.assertNotEqual(gateway.pk, self.gateway.pk)
        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.team, self.team)
        self.assertEqual(self.gateway.lifecycle_status, "released")
        mock_deprovision.assert_called_once()
        self.assertEqual(mock_deprovision.call_args.args[0].serial_number, self.serial)
        mock_provision.assert_called_once()


class CommissioningContextTest(TestCase):
    def setUp(self):
        from django.utils import timezone

        self.team = Team.objects.create(name="Commissioning", slug="commissioning")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Gateway",
            serial_number="GW-COMM-001",
            access_token="comm-token",
            status="offline",
            lifecycle_status="claimed",
        )
        self.template = DeviceTemplate.objects.create(
            name="Matched Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"active_power": {"unit": "W"}},
            is_verified=True,
        )
        self.now = timezone.now()

    def test_claimed_gateway_waits_for_connection(self):
        from apps.devices.services import build_commissioning_context

        context = build_commissioning_context(self.team, gateway=self.gateway)

        self.assertEqual(context["current_stage"], "gateway_connected")
        self.assertIn("gateway_claimed", context["completed_stages"])
        self.assertEqual(context["primary_action"]["label"], "Power on gateway")

    def test_discovery_result_keeps_gateway_connection_milestone_complete(self):
        from apps.devices.services import build_commissioning_context

        self.gateway.lifecycle_status = "commissioning"
        self.gateway.discovery_data = {
            "status": "complete",
            "devices": [
                {
                    "interface": "10.0.0.20:502",
                    "connection": "modbus_tcp",
                    "signature": "Unknown Modbus device",
                }
            ],
        }
        self.gateway.save(update_fields=["lifecycle_status", "discovery_data"])

        context = build_commissioning_context(self.team, gateway=self.gateway)

        self.assertEqual(context["gateway_state"].status, "offline")
        self.assertIn("gateway_connected", context["completed_stages"])
        self.assertEqual(context["current_stage"], "templates_selected")
        gateway_step = next(
            item for item in context["checklist"] if item["key"] == "gateway_connected"
        )
        self.assertTrue(gateway_step["complete"])
        self.assertFalse(gateway_step["current"])

    def test_online_gateway_with_discovery_splits_ready_and_needs_template(self):
        from django.utils import timezone

        from apps.devices.services import build_commissioning_context

        self.gateway.status = "online"
        self.gateway.last_seen = timezone.now()
        self.gateway.lifecycle_status = "commissioning"
        self.gateway.discovery_data = {
            "devices": [
                {
                    "interface": "10.0.0.2:502",
                    "signature": "Matched",
                    "matched_template_id": self.template.id,
                    "matched_template_score": 90,
                    "matched_template_reasons": ["model"],
                },
                {"interface": "10.0.0.3:502", "signature": "Unknown"},
            ]
        }
        self.gateway.save(update_fields=["status", "last_seen", "lifecycle_status", "discovery_data"])

        context = build_commissioning_context(self.team, gateway=self.gateway)

        self.assertEqual(len(context["ready_candidates"]), 1)
        self.assertEqual(len(context["needs_template_candidates"]), 1)
        self.assertEqual(context["current_stage"], "config_pushed")

    def test_config_push_and_first_telemetry_make_dashboard_ready(self):
        import uuid

        from django.utils import timezone

        from apps.devices.models import GatewayConfig
        from apps.devices.services import build_commissioning_context

        self.gateway.status = "online"
        self.gateway.last_seen = timezone.now()
        self.gateway.lifecycle_status = "commissioning"
        self.gateway.save(update_fields=["status", "last_seen", "lifecycle_status"])
        GatewayConfig.objects.create(
            team=self.team,
            gateway=self.gateway,
            config_json={"connectors": []},
            request_id=uuid.uuid4(),
            status="active",
        )
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Live Device",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_tcp",
            last_telemetry_at=timezone.now(),
        )

        context = build_commissioning_context(self.team, gateway=self.gateway)

        self.assertTrue(context["dashboard_ready"])
        self.assertEqual(context["first_live_device"], device)
        self.assertIn("config_pushed", context["completed_stages"])
