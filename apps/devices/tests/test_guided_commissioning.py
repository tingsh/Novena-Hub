import base64
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.devices.deployment_setup import (
    confidence_label,
    customer_safe_error,
    discovery_scan_state,
    gateway_readiness,
    get_or_create_setup_run,
    support_bundle,
    sync_setup_run,
)
from apps.devices.gateway_config_delivery import (
    dispatch_gateway_config_outbox,
    queue_gateway_config,
)
from apps.devices.models import (
    DeploymentSetupItem,
    Device,
    DeviceTemplate,
    EquipmentTemplateRequest,
    Gateway,
    GatewayConfig,
    GatewayConfigOutbox,
    RemoteCommand,
    RpcCommand,
    Site,
)
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser

SIGNING_SEED = base64.b64encode(b"0" * 32).decode()


@override_settings(
    REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID="setup-test",
    REMOTE_CONTROL_SIGNING_KEYS={"setup-test": SIGNING_SEED},
    REMOTE_CONTROL_SIGNING_PRIVATE_KEY=SIGNING_SEED,
)
class GatewayConfigDeliveryTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Guided Team", slug="guided-team")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Main Gateway",
            serial_number="NF-GUIDED-001",
            access_token="guided-token",
            gateway_capabilities=["guided_setup_v1"],
            status="online",
            mqtt_connected=True,
            last_seen=timezone.now(),
        )

    @patch("apps.devices.gateway_config_delivery._schedule_config_dispatch")
    def test_queue_builds_signed_revisioned_transactional_outbox(self, schedule):
        first = queue_gateway_config(self.gateway, "connector_update", {"connectors": []})
        second = queue_gateway_config(self.gateway, "connector_update", {"connectors": [{"type": "modbus"}]})

        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        first.refresh_from_db()
        self.assertEqual(first.status, "superseded")
        self.assertEqual(first.envelope_json["target"]["gateway_serial"], self.gateway.serial_number)
        self.assertEqual(first.envelope_json["checksum"], first.checksum)
        self.assertTrue(first.envelope_json["signature"])
        self.assertTrue(GatewayConfigOutbox.objects.filter(config=first, status="completed").exists())
        self.assertTrue(GatewayConfigOutbox.objects.filter(config=second, status="pending").exists())
        schedule.assert_not_called()

    @patch("apps.telemetry.mqtt_publisher.publish_config_envelope")
    @patch("apps.devices.gateway_config_delivery._schedule_config_dispatch")
    def test_dispatch_marks_broker_delivery_separately_from_activation(self, _schedule, publish):
        config = queue_gateway_config(self.gateway, "connector_update", {"connectors": []})

        dispatch_gateway_config_outbox(config.outbox.pk)

        config.refresh_from_db()
        config.outbox.refresh_from_db()
        self.assertEqual(config.status, "published")
        self.assertEqual(config.outbox.status, "awaiting_ack")
        self.assertIsNotNone(config.delivered_at)
        publish.assert_called_once()

    @patch("apps.telemetry.mqtt_publisher.publish_config_envelope", side_effect=RuntimeError("broker unavailable"))
    @patch("apps.devices.gateway_config_delivery._schedule_config_dispatch")
    def test_delivery_failure_is_retryable_and_customer_safe(self, _schedule, _publish):
        config = queue_gateway_config(self.gateway, "connector_update", {"connectors": []})

        dispatch_gateway_config_outbox(config.outbox.pk)

        config.refresh_from_db()
        config.outbox.refresh_from_db()
        self.assertEqual(config.status, "queued")
        self.assertEqual(config.outbox.status, "retry")
        self.assertEqual(config.error_code, "broker_publish_failed")
        self.assertNotIn("broker unavailable", config.error_message)


class DeploymentSetupWorkflowTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Workflow Team", slug="workflow-team")
        self.user = CustomUser.objects.create(email="setup@example.com", username="setup-user")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Gateway",
            serial_number="NF-WORKFLOW",
            access_token="workflow-token",
            status="online",
            mqtt_connected=True,
            tls_ok=True,
            firmware_version="1.0.0",
            last_seen=timezone.now(),
        )
        self.template = DeviceTemplate.objects.create(
            name="Verified Meter",
            manufacturer="MeterCo",
            model_number="M1",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1, "functionCode": 3, "type": "uint16", "unit": "V"}},
            is_verified=True,
        )

    def test_confidence_and_readiness_use_customer_facing_states(self):
        self.assertEqual(confidence_label(80), "High confidence")
        self.assertEqual(confidence_label(45), "Possible match")
        self.assertEqual(confidence_label(44), "Needs setup")
        self.assertEqual(gateway_readiness(self.gateway)["status"], "ready")

    def test_setup_failures_and_actions_do_not_require_broker_or_modbus_knowledge(self):
        self.gateway.mqtt_connected = False
        self.gateway.save(update_fields=["mqtt_connected"])
        readiness = gateway_readiness(self.gateway)
        cloud_check = next(item for item in readiness["checks"] if item["key"] == "mqtt_connected")

        self.assertEqual(readiness["status"], "blocked")
        self.assertIn("site internet connection", cloud_check["action"])
        self.assertNotIn("broker", cloud_check["action"].lower())
        self.assertNotIn("Modbus", customer_safe_error("connection refused"))
        self.assertIn("clock is out of sync", customer_safe_error("Diagnostic command was issued in the future"))
        self.assertIn("clock is out of sync", customer_safe_error("Diagnostic command timestamp is outside its trusted window"))

    def test_successful_validation_then_telemetry_completes_run_and_dashboard(self):
        run = get_or_create_setup_run(team=self.team, gateway=self.gateway, initiated_by=self.user)
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Meter",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_tcp",
            metadata={"guided_setup_validation": "pending"},
        )
        command = RemoteCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            requested_by=self.user,
            operation="deployment_validate",
            risk="diagnostic",
            status="action_completed",
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        item = DeploymentSetupItem.objects.create(
            team=self.team,
            run=run,
            device=device,
            selected_template=self.template,
            state="validating",
            validation_command=command,
        )
        RpcCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            request_id=uuid.uuid4(),
            method="deployment_validate",
            status="success",
            result={
                "status": "success",
                "message": "Voltage was read successfully.",
                "signals": [{"key": "voltage", "status": "success"}],
            },
            remote_command=command,
        )

        sync_setup_run(run)
        item.refresh_from_db()
        device.refresh_from_db()
        self.assertEqual(item.state, "validated")
        self.assertEqual(device.metadata["guided_setup_validation"], "validated")

        GatewayConfig.objects.create(
            team=self.team,
            gateway=self.gateway,
            setup_run=run,
            config_json={"connectors": []},
            request_id=uuid.uuid4(),
            status="active",
        )
        device.last_telemetry_at = timezone.now()
        device.save(update_fields=["last_telemetry_at"])
        item.state = "applied"
        item.save(update_fields=["state"])

        completed = sync_setup_run(run)
        item.refresh_from_db()
        self.assertEqual(item.state, "telemetry_confirmed")
        self.assertEqual(completed.state, "completed")
        self.assertTrue(device.dashboards.exists())

    def test_support_bundle_excludes_raw_configuration_and_credentials(self):
        run = get_or_create_setup_run(team=self.team, gateway=self.gateway, initiated_by=self.user)
        config = GatewayConfig.objects.create(
            team=self.team,
            gateway=self.gateway,
            setup_run=run,
            config_json={"mqtt": {"password": "secret-value"}},
            request_id=uuid.uuid4(),
            checksum="abc",
            status="failed",
            error_message="Connection timed out.",
            technical_error="password=secret-value",
        )

        bundle = support_bundle(run)

        self.assertEqual(bundle["configuration"]["request_id"], str(config.request_id))
        self.assertNotIn("config_json", bundle["configuration"])
        self.assertNotIn("secret-value", str(bundle))
        self.assertIn("[redacted]", str(bundle))

    @override_settings(GUIDED_SETUP_FIRST_TELEMETRY_TIMEOUT_SECONDS=1)
    def test_multi_device_setup_completes_with_attention_after_telemetry_timeout(self):
        run = get_or_create_setup_run(team=self.team, gateway=self.gateway, initiated_by=self.user)
        live_device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Live meter",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_tcp",
            last_telemetry_at=timezone.now(),
        )
        quiet_device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Quiet meter",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_tcp",
        )
        DeploymentSetupItem.objects.create(
            team=self.team,
            run=run,
            device=live_device,
            selected_template=self.template,
            state="applied",
        )
        quiet_item = DeploymentSetupItem.objects.create(
            team=self.team,
            run=run,
            device=quiet_device,
            selected_template=self.template,
            state="applied",
        )
        GatewayConfig.objects.create(
            team=self.team,
            gateway=self.gateway,
            setup_run=run,
            config_json={"connectors": []},
            request_id=uuid.uuid4(),
            status="active",
            acknowledged_at=timezone.now() - timedelta(seconds=5),
        )

        updated = sync_setup_run(run)

        quiet_item.refresh_from_db()
        self.assertEqual(quiet_item.state, "needs_attention")
        self.assertEqual(updated.state, "completed_attention")


class GuidedSetupViewTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="View Team", slug="view-team")
        self.user = CustomUser.objects.create(email="view@example.com", username="view-user")
        Membership.objects.create(team=self.team, user=self.user, role="admin")
        self.site = Site.objects.create(team=self.team, name="Factory")
        self.gateway = Gateway.objects.create(
            team=self.team,
            site=self.site,
            name="Gateway",
            serial_number="NF-VIEW",
            access_token="view-token",
            status="online",
            last_seen=timezone.now(),
        )
        self.client = Client()
        self.client.force_login(self.user)
        session = self.client.session
        session["onboarding_site_id"] = self.site.pk
        session["onboarding_gateway_id"] = self.gateway.pk
        session.save()
        self.url = reverse("web_team:onboarding:step_3_discover", args=[self.team.slug])

    def _enable_guided_setup(self):
        self.gateway.gateway_capabilities = ["guided_setup_v1"]
        self.gateway.save(update_fields=["gateway_capabilities"])

    def test_gateway_wait_refreshes_stale_blocked_readiness_after_heartbeat(self):
        self.gateway.status = "offline"
        self.gateway.mqtt_connected = False
        self.gateway.last_seen = None
        self.gateway.save(update_fields=["status", "mqtt_connected", "last_seen"])
        run = get_or_create_setup_run(team=self.team, gateway=self.gateway, initiated_by=self.user)
        run.readiness = gateway_readiness(self.gateway)
        run.save(update_fields=["readiness", "updated_at"])
        self.assertEqual(run.readiness["status"], "blocked")

        self.gateway.status = "online"
        self.gateway.mqtt_connected = True
        self.gateway.tls_ok = True
        self.gateway.last_seen = timezone.now()
        self.gateway.save(update_fields=["status", "mqtt_connected", "tls_ok", "last_seen"])

        response = self.client.get(reverse("web_team:onboarding:step_2b_wait", args=[self.team.slug]))

        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.readiness["status"], "ready")
        self.assertContains(response, "Continue to Equipment")
        self.assertContains(response, 'hx-disinherit="hx-target hx-select"')
        self.assertContains(response, 'hx-target="this"')

    def _scan_run(self, scan_id="scan-current"):
        run = get_or_create_setup_run(
            team=self.team,
            gateway=self.gateway,
            initiated_by=self.user,
        )
        run.state = run.State.DISCOVERING
        run.current_step = "equipment"
        run.summary = {
            **(run.summary or {}),
            "discovery": {"active_scan_id": scan_id},
        }
        run.save(update_fields=["state", "current_step", "summary", "updated_at"])
        return run

    @override_settings(
        REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID="setup-test",
        REMOTE_CONTROL_SIGNING_KEYS={"setup-test": SIGNING_SEED},
        REMOTE_CONTROL_SIGNING_PRIVATE_KEY=SIGNING_SEED,
    )
    @patch("apps.devices.remote_control._schedule_outbox_dispatch")
    def test_scan_button_creates_correlated_hardened_command(self, schedule):
        self._enable_guided_setup()

        response = self.client.post(self.url, {"action": "start_discovery"})

        self.assertEqual(response.status_code, 302)
        command = RemoteCommand.objects.get(gateway=self.gateway, operation="deployment_discover")
        scan_id = command.request_payload["params"]["scan_id"]
        self.assertEqual(command.request_payload["params"]["scope"], "attached_interfaces")
        self.assertEqual(command.risk, "diagnostic")
        from apps.devices.remote_control_crypto import build_signed_command_envelope

        envelope = build_signed_command_envelope(command, request_id=uuid.uuid4())
        self.assertEqual(envelope["method"], "deployment_discover")
        self.assertEqual(envelope["params"]["scan_id"], scan_id)
        self.assertEqual(envelope["target"]["gateway_serial"], self.gateway.serial_number)
        self.assertEqual(envelope["signing_key_id"], "setup-test")
        self.assertTrue(envelope["signature"])
        run = self.gateway.deployment_setup_runs.get()
        self.assertEqual(run.summary["discovery"]["active_scan_id"], scan_id)
        self.assertEqual(run.summary["discovery"]["command_id"], str(command.pk))
        self.assertIn("visible_until", run.summary["discovery"])
        self.assertEqual(run.state, run.State.DISCOVERING)
        schedule.assert_not_called()

    @override_settings(
        REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID="setup-test",
        REMOTE_CONTROL_SIGNING_KEYS={"setup-test": SIGNING_SEED},
        REMOTE_CONTROL_SIGNING_PRIVATE_KEY=SIGNING_SEED,
    )
    @patch("apps.devices.remote_control._schedule_outbox_dispatch")
    def test_specific_endpoint_scan_is_exact_bounded_and_supports_custom_port(self, schedule):
        self._enable_guided_setup()

        page = self.client.get(self.url)
        self.assertContains(page, "Advanced settings")
        self.assertContains(page, "Check equipment using a known network address")
        self.assertContains(page, "Most customers do not need this")
        self.assertNotContains(page, "simulator port")
        self.assertContains(page, 'name="target_port"')

        response = self.client.post(
            self.url,
            {
                "action": "start_target_discovery",
                "target_host": "10.0.0.20",
                "target_port": "1502",
            },
        )

        self.assertEqual(response.status_code, 302)
        command = RemoteCommand.objects.get(gateway=self.gateway, operation="deployment_discover")
        self.assertEqual(
            command.request_payload["params"],
            {
                "scan_id": command.request_payload["params"]["scan_id"],
                "scope": "approved_targets",
                "tcp_hosts": [{"host": "10.0.0.20", "port": 1502}],
                "serial_ports": [],
            },
        )
        run = self.gateway.deployment_setup_runs.get()
        self.assertEqual(run.summary["discovery"]["mode"], "approved_target")
        self.assertIn("10.0.0.20:1502", run.summary["discovery"]["scope_label"])
        schedule.assert_not_called()

    @patch("apps.devices.remote_control._schedule_outbox_dispatch")
    def test_specific_endpoint_scan_rejects_loopback(self, _schedule):
        self._enable_guided_setup()

        response = self.client.post(
            self.url,
            {
                "action": "start_target_discovery",
                "target_host": "127.0.0.1",
                "target_port": "502",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(RemoteCommand.objects.filter(gateway=self.gateway).exists())

    @patch("apps.devices.remote_control.request_remote_command", side_effect=RuntimeError("broker down"))
    def test_scan_button_keeps_visible_state_when_command_dispatch_fails(self, _request_command):
        self._enable_guided_setup()

        response = self.client.post(self.url, {"action": "start_discovery"})

        self.assertEqual(response.status_code, 302)
        run = self.gateway.deployment_setup_runs.get()
        self.assertIn("visible_until", run.summary["discovery"])
        self.assertEqual(discovery_scan_state(run)["title"], "Scanning connected equipment")

    def test_scan_states_render_idle_running_found_empty_and_error(self):
        self._enable_guided_setup()
        idle = self.client.get(self.url)
        self.assertContains(idle, "Ready to scan")
        self.assertContains(idle, "Scan for equipment")
        self.assertContains(idle, "field-side wired Ethernet")
        self.assertContains(idle, "Modbus RTU")
        self.assertContains(idle, 'hx-disinherit="hx-target hx-select"')
        self.assertContains(idle, 'hx-target="this"')
        self.assertContains(idle, 'hx-target="#scan-state-panel"')
        self.assertContains(idle, 'hx-sync="#scan-state-panel:replace"')

        run = self._scan_run()
        cases = [
            (
                {"scan_id": "scan-current", "status": "running", "progress": {"completed": 7, "total": 253}},
                "Scanning connected equipment",
            ),
            (
                {
                    "scan_id": "scan-current",
                    "status": "running",
                    "progress": {"completed": 94, "total": 508},
                    "received_at": (timezone.now() - timedelta(minutes=4)).isoformat(),
                },
                "Scan timed out",
            ),
            (
                {
                    "scan_id": "scan-current",
                    "status": "complete",
                    "devices": [{"interface": "10.0.0.20:502"}],
                },
                "Found 1 device",
            ),
            ({"scan_id": "scan-current", "status": "complete", "devices": []}, "No devices found"),
            ({"scan_id": "scan-current", "status": "error", "errors": [{"error": "probe failed"}]}, "Scan failed"),
        ]
        for report, expected in cases:
            with self.subTest(expected=expected):
                self.gateway.discovery_data = report
                self.gateway.save(update_fields=["discovery_data"])
                run.refresh_from_db()
                self.assertEqual(discovery_scan_state(run)["title"], expected)
                response = self.client.get(self.url)
                self.assertContains(response, expected)

        self.gateway.discovery_data = {
            "scan_id": "scan-current",
            "status": "running",
            "progress": {"completed": 122, "total": 508},
        }
        self.gateway.save(update_fields=["discovery_data"])
        response = self.client.get(self.url)
        self.assertContains(response, 'id="scan-state-panel"')
        self.assertContains(response, 'hx-select="#scan-state-panel"')
        self.assertContains(response, 'hx-trigger="every 2s"')

        self.gateway.discovery_data = {
            "scan_id": "scan-current",
            "status": "complete",
            "devices": [{"interface": "10.0.0.20:502", "connection": "modbus_tcp"}],
        }
        self.gateway.save(update_fields=["discovery_data"])
        response = self.client.get(self.url)
        self.assertContains(response, 'hx-trigger="none"')
        self.assertContains(response, 'id="advanced-discovery-settings" hx-preserve')

    def test_discovery_renders_many_results_as_a_compact_equipment_checklist(self):
        self._enable_guided_setup()
        self.gateway.discovery_data = {
            "status": "complete",
            "devices": [
                {
                    "interface": f"10.0.0.{index + 20}:502",
                    "connection": "modbus_tcp",
                    "signature": f"Unknown device {index + 1}",
                }
                for index in range(5)
            ],
        }
        self.gateway.save(update_fields=["discovery_data"])

        response = self.client.get(self.url)

        self.assertContains(response, "Equipment checklist")
        self.assertContains(response, 'role="table" aria-label="Discovered equipment"')
        self.assertContains(response, 'data-equipment-row="candidate"', count=5)
        self.assertContains(response, "0 of 5 validated")
        self.assertContains(response, "5 equipment items still need review")
        self.assertContains(response, "Review this equipment before validation", count=5)
        self.assertContains(response, '@click.outside="searchOpen = false"', count=5)
        self.assertContains(response, '@keydown.escape.window="searchOpen = false"', count=5)
        self.assertContains(response, 'x-show="searchOpen" x-cloak', count=5)
        self.assertNotContains(response, "High-confidence matches")

    def test_discovered_equipment_name_can_be_saved_without_a_template(self):
        self._enable_guided_setup()
        self.gateway.discovery_data = {
            "status": "complete",
            "devices": [
                {
                    "interface": "10.0.0.20:502",
                    "connection": "modbus_tcp",
                    "host": "10.0.0.20",
                    "port": 502,
                    "signature": "Unknown device",
                }
            ],
        }
        self.gateway.save(update_fields=["discovery_data"])

        response = self.client.post(
            self.url,
            {
                "action": "validate_selected",
                "save_candidate_draft": "0",
                "name_0": "Main incomer meter",
                "template_0": "",
                "host_0": "10.0.0.20",
                "port_0": "502",
                "slave_id_0": "7",
            },
        )

        self.assertEqual(response.status_code, 302)
        item = DeploymentSetupItem.objects.get(run__gateway=self.gateway, discovery_index=0)
        self.assertIsNone(item.device)
        self.assertIsNone(item.selected_template)
        self.assertEqual(item.state, DeploymentSetupItem.State.DISCOVERED)
        self.assertEqual(item.candidate_data["customer_name"], "Main incomer meter")
        self.assertEqual(item.connection["slave_id"], 7)
        self.assertFalse(Device.objects.filter(gateway=self.gateway).exists())

        page = self.client.get(self.url)
        self.assertContains(page, "Draft saved · Needs template")
        self.assertContains(page, 'name="name_0" value="Main incomer meter"')
        self.assertContains(page, "equipmentName: 'Main incomer meter'")
        self.assertContains(page, 'x-text="equipmentName"')
        self.assertContains(page, 'name="save_candidate_draft" value="0"')
        self.assertContains(page, "Save draft")

        self.gateway.discovery_data = {"status": "complete", "devices": []}
        self.gateway.save(update_fields=["discovery_data"])

        retained_page = self.client.get(self.url)
        self.assertEqual(retained_page.status_code, 200)
        self.assertContains(retained_page, "Main incomer meter")
        self.assertContains(retained_page, 'data-equipment-row="candidate"')
        self.assertNotContains(retained_page, 'data-equipment-row="configured"')

        poll_url = reverse("web_team:onboarding:discovery_poll", args=[self.team.slug])
        retained_poll = self.client.get(poll_url)
        self.assertEqual(retained_poll.status_code, 200)
        self.assertContains(retained_poll, "Main incomer meter")

        custom_builder = self.client.post(
            self.url,
            {
                "action": "validate_selected",
                "start_custom_template": "0",
                "name_0": "Main incomer meter",
                "template_0": "",
                "host_0": "10.0.0.20",
                "port_0": "502",
                "slave_id_0": "7",
            },
        )
        self.assertEqual(custom_builder.status_code, 302)
        self.assertIn("workflow=custom", custom_builder.url)

    def test_candidate_template_search_is_actionable_and_draft_can_open_custom_builder(self):
        self._enable_guided_setup()
        template = DeviceTemplate.objects.create(
            name="Schneider PM5560",
            manufacturer="Schneider Electric",
            model_number="PM5560",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1, "functionCode": 3, "type": "uint16"}},
            is_verified=True,
        )
        self.gateway.discovery_data = {
            "status": "complete",
            "devices": [
                {
                    "interface": "10.0.0.20:502",
                    "connection": "modbus_tcp",
                    "host": "10.0.0.20",
                    "port": 502,
                    "signature": "Unknown device",
                }
            ],
        }
        self.gateway.save(update_fields=["discovery_data"])
        search_url = reverse("web_team:devices:template_library_search", args=[self.team.slug])

        search = self.client.get(
            search_url,
            {"context": "guided_setup", "candidate_index": "0", "q": "Schneider PM"},
        )

        self.assertContains(search, template.name)
        self.assertContains(search, "template-picked")
        self.assertContains(search, "Novena verified")

        redirect_to_builder = self.client.post(
            self.url,
            {
                "action": "validate_selected",
                "start_custom_template": "0",
                "name_0": "Main incomer meter",
                "template_0": "",
                "host_0": "10.0.0.20",
                "port_0": "1502",
                "slave_id_0": "7",
            },
        )

        self.assertEqual(redirect_to_builder.status_code, 302)
        self.assertIn("workflow=custom", redirect_to_builder.url)
        builder = self.client.get(redirect_to_builder.url)
        self.assertContains(builder, 'id="manual-device" open')
        self.assertContains(builder, 'id="custom-template"')
        self.assertContains(builder, 'name="manual_name" value="Main incomer meter"')
        self.assertContains(builder, 'name="manual_host" value="10.0.0.20"')
        self.assertContains(builder, 'name="manual_port" value="1502"')
        self.assertContains(builder, 'name="manual_slave_id" value="7"')
        self.assertContains(builder, "Create private template and review signals")

        created = self.client.post(
            self.url,
            {
                "action": "manual",
                "draft_index": "0",
                "manual_name": "Main incomer meter",
                "manual_protocol": "modbus_tcp",
                "manual_manufacturer": "Omron",
                "manual_model": "KM-N2-FLK",
                "manual_device_type": "power_meter",
                "manual_host": "10.0.0.20",
                "manual_port": "1502",
                "manual_slave_id": "7",
                "manual_timeout": "3",
                "point_key_1": "voltage",
                "point_label_1": "Line voltage",
                "point_address_1": "0",
                "point_function_1": "3",
                "point_type_1": "uint16",
                "point_scale_1": "1",
                "point_unit_1": "V",
            },
        )

        self.assertEqual(created.status_code, 302)
        private_template = DeviceTemplate.objects.get(
            created_by_team=self.team,
            manufacturer="Omron",
            model_number="KM-N2-FLK",
        )
        self.assertFalse(private_template.is_verified)
        self.assertEqual(private_template.source, "user_created")
        item = DeploymentSetupItem.objects.get(run__gateway=self.gateway, discovery_index=0)
        self.assertEqual(item.selected_template, private_template)
        self.assertIsNotNone(item.device)
        self.assertEqual(item.device.connection_config["port"], 1502)
        self.assertEqual(
            DeploymentSetupItem.objects.filter(run__gateway=self.gateway, discovery_index=0).count(),
            1,
        )

    def test_template_request_is_linked_back_to_saved_candidate(self):
        self._enable_guided_setup()
        self.gateway.discovery_data = {
            "status": "complete",
            "devices": [
                {
                    "interface": "10.0.0.20:502",
                    "connection": "modbus_tcp",
                    "host": "10.0.0.20",
                    "port": 502,
                    "signature": "Unknown device",
                }
            ],
        }
        self.gateway.save(update_fields=["discovery_data"])
        self.client.post(
            self.url,
            {
                "action": "validate_selected",
                "request_candidate_template": "0",
                "name_0": "Packaging meter",
                "template_0": "",
                "host_0": "10.0.0.20",
                "port_0": "502",
                "slave_id_0": "1",
            },
        )

        response = self.client.post(
            self.url,
            {
                "action": "request_template",
                "draft_index": "0",
                "request_manufacturer": "Eaton",
                "request_model": "Power Xpert 2000",
                "request_protocol": "modbus_tcp",
            },
        )

        self.assertEqual(response.status_code, 302)
        request_row = EquipmentTemplateRequest.objects.get(team=self.team, model_number="Power Xpert 2000")
        item = DeploymentSetupItem.objects.get(run__gateway=self.gateway, discovery_index=0)
        self.assertEqual(
            item.candidate_data["template_request_reference"],
            str(request_row.support_reference),
        )
        page = self.client.get(self.url)
        self.assertContains(page, "Template requested")
        self.assertContains(page, str(request_row.support_reference))

    def test_validation_poll_updates_row_completion_and_next_step(self):
        self._enable_guided_setup()
        template = DeviceTemplate.objects.create(
            name="Validated meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1, "functionCode": 3, "type": "uint16"}},
            is_verified=True,
        )
        device = Device.objects.create(
            team=self.team,
            gateway=self.gateway,
            site=self.site,
            name="Main meter",
            template=template,
            device_type="power_meter",
            protocol="modbus_tcp",
            port="10.0.0.20:502",
        )
        run = get_or_create_setup_run(team=self.team, gateway=self.gateway, initiated_by=self.user)
        item = DeploymentSetupItem.objects.create(
            team=self.team,
            run=run,
            device=device,
            selected_template=template,
            state=DeploymentSetupItem.State.VALIDATING,
            candidate_data={"interface": "10.0.0.20:502", "connection": "modbus_tcp"},
        )
        poll_url = reverse("web_team:onboarding:discovery_poll", args=[self.team.slug])

        validating = self.client.get(poll_url)

        self.assertContains(validating, 'hx-trigger="every 2s"')
        self.assertContains(validating, "Validating")
        self.assertContains(validating, 'id="deployment-preview" hx-swap-oob="true"')

        item.state = DeploymentSetupItem.State.VALIDATED
        item.validation_result = {"status": "success", "message": "Selected signals were read successfully."}
        item.save(update_fields=["state", "validation_result", "updated_at"])
        complete = self.client.get(poll_url)

        self.assertContains(complete, "1 of 1 validated")
        self.assertContains(complete, 'aria-label="Validation complete"')
        self.assertContains(complete, "Deploy and continue")
        self.assertContains(complete, 'hx-trigger="none"')

    def test_recent_scan_start_keeps_customer_visible_scanning_state(self):
        self._enable_guided_setup()
        run = self._scan_run()
        run.summary = {
            **(run.summary or {}),
            "discovery": {
                **((run.summary or {}).get("discovery") or {}),
                "visible_until": (timezone.now() + timedelta(seconds=5)).isoformat(),
            },
        }
        run.save(update_fields=["summary", "updated_at"])

        for report in [
            {"scan_id": "scan-current", "status": "complete", "devices": []},
            {"scan_id": "scan-current", "status": "error", "errors": [{"error": "probe failed"}]},
        ]:
            with self.subTest(status=report["status"]):
                self.gateway.discovery_data = report
                self.gateway.save(update_fields=["discovery_data"])
                run.refresh_from_db()

                state = discovery_scan_state(run)
                response = self.client.get(self.url)

                self.assertEqual(state["key"], "scanning")
                self.assertEqual(state["title"], "Scanning connected equipment")
                self.assertContains(response, "Scanning connected equipment")

    def test_stale_terminal_report_cannot_complete_retry(self):
        run = self._scan_run(scan_id="scan-retry")
        self.gateway.discovery_data = {
            "scan_id": "scan-old",
            "status": "complete",
            "devices": [{"interface": "10.0.0.20:502"}],
        }
        self.gateway.save(update_fields=["discovery_data"])

        synced = sync_setup_run(run)

        self.assertEqual(synced.state, synced.State.DISCOVERING)
        self.assertEqual(discovery_scan_state(synced)["title"], "Scanning connected equipment")

    @patch("apps.devices.remote_control._schedule_outbox_dispatch")
    def test_retry_uses_a_new_scan_id(self, _schedule):
        self._enable_guided_setup()
        self.client.post(self.url, {"action": "start_discovery"})
        run = self.gateway.deployment_setup_runs.get()
        first_scan_id = run.summary["discovery"]["active_scan_id"]
        self.gateway.discovery_data = {"scan_id": first_scan_id, "status": "complete", "devices": []}
        self.gateway.save(update_fields=["discovery_data"])
        sync_setup_run(run)

        self.client.post(self.url, {"action": "start_discovery"})

        run.refresh_from_db()
        self.assertNotEqual(run.summary["discovery"]["active_scan_id"], first_scan_id)
        self.assertEqual(RemoteCommand.objects.filter(gateway=self.gateway, operation="deployment_discover").count(), 2)

    @patch("apps.devices.remote_control._schedule_outbox_dispatch")
    def test_retry_after_rejected_scan_creates_fresh_command(self, _schedule):
        self._enable_guided_setup()
        self.client.post(self.url, {"action": "start_discovery"})
        run = self.gateway.deployment_setup_runs.get()
        first_scan_id = run.summary["discovery"]["active_scan_id"]
        command = RemoteCommand.objects.get(gateway=self.gateway, operation="deployment_discover")
        command.status = RemoteCommand.Status.REJECTED
        command.error_message = "Diagnostic command timestamp is outside its trusted window"
        command.save(update_fields=["status", "error_message", "updated_at"])

        response = self.client.post(self.url, {"action": "start_discovery"})

        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        self.assertNotEqual(run.summary["discovery"]["active_scan_id"], first_scan_id)
        self.assertEqual(RemoteCommand.objects.filter(gateway=self.gateway, operation="deployment_discover").count(), 2)

    @patch("apps.devices.remote_control._schedule_outbox_dispatch")
    def test_retry_during_running_scan_does_not_start_duplicate_command(self, _schedule):
        self._enable_guided_setup()
        run = self._scan_run(scan_id="scan-running")
        self.gateway.discovery_data = {
            "scan_id": "scan-running",
            "status": "running",
            "progress": {"completed": 123, "total": 508},
        }
        self.gateway.save(update_fields=["discovery_data"])

        response = self.client.post(self.url, {"action": "start_discovery"})

        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        self.assertEqual(run.summary["discovery"]["active_scan_id"], "scan-running")
        self.assertEqual(RemoteCommand.objects.filter(gateway=self.gateway, operation="deployment_discover").count(), 0)

    @patch("apps.devices.remote_control._schedule_outbox_dispatch")
    def test_retry_reattaches_to_gateway_running_scan_instead_of_duplicate_command(self, _schedule):
        self._enable_guided_setup()
        run = self._scan_run(scan_id="failed-duplicate")
        self.gateway.discovery_data = {
            "scan_id": "gateway-running",
            "status": "running",
            "progress": {"completed": 87, "total": 508},
        }
        self.gateway.save(update_fields=["discovery_data"])

        response = self.client.post(self.url, {"action": "start_discovery"})

        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        self.assertEqual(run.summary["discovery"]["active_scan_id"], "gateway-running")
        self.assertIn("reattached_at", run.summary["discovery"])
        self.assertNotIn("command_id", run.summary["discovery"])
        self.assertEqual(RemoteCommand.objects.filter(gateway=self.gateway, operation="deployment_discover").count(), 0)

    def test_matching_rpc_failure_becomes_scan_failed(self):
        run = self._scan_run()
        command = RemoteCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            requested_by=self.user,
            operation="deployment_discover",
            risk="diagnostic",
            request_payload={"method": "deployment_discover", "params": {"scan_id": "scan-current"}},
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        summary = dict(run.summary)
        summary["discovery"]["command_id"] = str(command.pk)
        run.summary = summary
        run.save(update_fields=["summary", "updated_at"])
        RpcCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            request_id=uuid.uuid4(),
            method="deployment_discover",
            params={"scan_id": "scan-current"},
            status="timeout",
            error_message="Gateway did not respond",
            remote_command=command,
        )

        state = discovery_scan_state(run)

        self.assertEqual(state["title"], "Scan failed")

    def test_gateway_already_running_response_stays_customer_visible_scanning(self):
        run = self._scan_run()
        command = RemoteCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            requested_by=self.user,
            operation="deployment_discover",
            risk="diagnostic",
            request_payload={"method": "deployment_discover", "params": {"scan_id": "scan-current"}},
            status=RemoteCommand.Status.FAILED,
            error_message="A discovery scan is already running",
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        summary = dict(run.summary)
        summary["discovery"]["command_id"] = str(command.pk)
        run.summary = summary
        run.save(update_fields=["summary", "updated_at"])

        state = discovery_scan_state(run)

        self.assertEqual(state["key"], "scanning")
        self.assertEqual(state["title"], "Gateway is already scanning")

    def test_mqtt_progress_is_cached_and_older_reports_are_ignored(self):
        from apps.telemetry.management.commands.mqtt_consumer import Command

        consumer = Command()
        consumer._process_discovery_report(
            self.gateway,
            {
                "schema_version": 1,
                "scan_id": "scan-current",
                "scan_ts": 200,
                "status": "running",
                "phase": "scanning_ethernet",
                "progress": {"completed": 20, "total": 253},
                "discovered_devices": [],
            },
        )
        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.discovery_data["progress"]["completed"], 20)

        consumer._process_discovery_report(
            self.gateway,
            {
                "scan_id": "scan-old",
                "scan_ts": 100,
                "status": "complete",
                "discovered_devices": [{"interface": "10.0.0.99:502"}],
            },
        )
        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.discovery_data["scan_id"], "scan-current")

        consumer._process_discovery_report(
            self.gateway,
            {
                "scan_id": "scan-current",
                "scan_ts": 200,
                "updated_at": 300,
                "status": "complete",
                "discovered_devices": [],
            },
        )
        self.gateway.refresh_from_db()
        consumer._process_discovery_report(
            self.gateway,
            {
                "scan_id": "scan-current",
                "scan_ts": 200,
                "updated_at": 250,
                "status": "running",
                "discovered_devices": [],
            },
        )
        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.discovery_data["status"], "complete")

    def test_active_guided_scan_report_can_replace_future_cached_report(self):
        from apps.telemetry.management.commands.mqtt_consumer import Command

        run = self._scan_run(scan_id="scan-current")
        self.gateway.discovery_data = {
            "scan_id": "scan-old",
            "scan_ts": 999_999,
            "status": "complete",
            "devices": [{"interface": "10.0.0.99:502"}],
        }
        self.gateway.save(update_fields=["discovery_data"])

        Command()._process_discovery_report(
            self.gateway,
            {
                "schema_version": 1,
                "scan_id": "scan-current",
                "scan_ts": 100,
                "scan_type": "guided",
                "status": "complete",
                "phase": "complete",
                "progress": {"completed": 508, "total": 508},
                "discovered_devices": [],
            },
        )

        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.discovery_data["scan_id"], "scan-current")
        self.assertEqual(discovery_scan_state(run)["title"], "No devices found")
        synced = sync_setup_run(run)
        self.assertEqual(synced.state, synced.State.CONFIGURING)

    def test_legacy_guided_discovery_report_without_scan_id_completes_active_scan(self):
        from apps.telemetry.management.commands.mqtt_consumer import Command

        run = self._scan_run(scan_id="scan-current")
        consumer = Command()

        consumer._process_discovery_report(
            self.gateway,
            {
                "schema_version": 0,
                "scan_id": "",
                "scan_ts": 200,
                "scan_type": "guided",
                "status": "complete",
                "interfaces": [{"name": "/dev/ttyAMA3", "type": "serial"}],
                "discovered_devices": [],
            },
        )

        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.discovery_data["scan_id"], "scan-current")
        self.assertEqual(discovery_scan_state(run)["title"], "No devices found")
        synced = sync_setup_run(run)
        self.assertEqual(synced.state, synced.State.CONFIGURING)

    def test_page_exposes_manual_fallback_without_raw_json(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "I can’t find my equipment")
        self.assertContains(response, "Guided Modbus setup")
        self.assertNotContains(response, "connection_config")
        self.assertNotContains(response, "raw connector JSON")

    @patch("apps.devices.deployment_setup.start_validation")
    def test_reachable_endpoint_can_override_port_and_unit_before_template_validation(self, validation):
        self._enable_guided_setup()
        template = DeviceTemplate.objects.create(
            name="Simulator template",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={
                "voltage": {
                    "address": 42,
                    "functionCode": 4,
                    "type": "uint16",
                }
            },
            is_verified=True,
        )
        self.gateway.discovery_data = {
            "status": "complete",
            "devices": [
                {
                    "interface": "10.0.0.20:1502",
                    "connection": "modbus_tcp",
                    "host": "10.0.0.20",
                    "port": 1502,
                    "signature": "Reachable TCP endpoint",
                    "protocol_verified": False,
                }
            ],
        }
        self.gateway.save(update_fields=["discovery_data"])

        page = self.client.get(self.url)
        self.assertContains(page, "TCP endpoint reachable; Modbus unit and register map are not yet verified.")
        self.assertContains(page, 'name="port_0" value="1502"')
        self.assertContains(page, 'name="slave_id_0" value="1"')

        response = self.client.post(
            self.url,
            {
                "action": "validate_selected",
                "device_index": ["0"],
                "name_0": "Laptop 2 simulator",
                "template_0": str(template.pk),
                "host_0": "10.0.0.20",
                "port_0": "5020",
                "slave_id_0": "7",
            },
        )

        self.assertEqual(response.status_code, 302)
        item = DeploymentSetupItem.objects.get(run__gateway=self.gateway)
        self.assertEqual(item.connection["host"], "10.0.0.20")
        self.assertEqual(item.connection["port"], 5020)
        self.assertEqual(item.connection["slave_id"], 7)
        self.assertEqual(item.device.connection_config, item.connection)
        validation.assert_called_once()

    @patch("apps.devices.deployment_setup.start_validation")
    def test_manual_setup_creates_private_unverified_template(self, _validation):
        self.gateway.gateway_capabilities = ["guided_setup_v1"]
        self.gateway.save(update_fields=["gateway_capabilities"])
        response = self.client.post(
            self.url,
            {
                "action": "manual",
                "manual_name": "Main Meter",
                "manual_protocol": "modbus_tcp",
                "manual_manufacturer": "PrivateCo",
                "manual_model": "P1",
                "manual_device_type": "power_meter",
                "manual_host": "10.0.0.20",
                "manual_port": "502",
                "manual_slave_id": "1",
                "manual_timeout": "3",
                "manual_byte_order": "BIG",
                "manual_word_order": "BIG",
                "point_key_1": "voltage",
                "point_address_1": "1",
                "point_type_1": "uint16",
                "point_function_1": "3",
                "point_count_1": "1",
                "point_scale_1": "1",
                "point_unit_1": "V",
            },
        )

        self.assertEqual(response.status_code, 302)
        template = DeviceTemplate.objects.get(created_by_team=self.team, model_number="P1")
        self.assertFalse(template.is_verified)
        self.assertEqual(template.source, "user_created")
        device = Device.objects.get(team=self.team, name="Main Meter")
        self.assertEqual(device.connection_config["host"], "10.0.0.20")
        self.assertEqual(device.metadata["guided_setup_validation"], "pending")

    @patch("apps.devices.deployment_setup.start_validation")
    def test_manual_setup_enforces_team_device_limit(self, validation):
        self.gateway.gateway_capabilities = ["guided_setup_v1"]
        self.gateway.save(update_fields=["gateway_capabilities"])
        for index in range(3):
            Device.objects.create(
                team=self.team,
                site=self.site,
                gateway=self.gateway,
                name=f"Existing Device {index}",
                device_type="power_meter",
                protocol="modbus_tcp",
            )

        response = self.client.post(
            self.url,
            {
                "action": "manual",
                "manual_name": "Over Limit Meter",
                "manual_protocol": "modbus_tcp",
                "manual_manufacturer": "PrivateCo",
                "manual_model": "LIMIT",
                "manual_device_type": "power_meter",
                "manual_host": "192.168.1.50",
                "manual_port": "502",
                "point_key_1": "voltage",
                "point_address_1": "1",
            },
            follow=True,
        )

        self.assertContains(response, "Your current plan supports up to 3 equipment items")
        self.assertFalse(Device.objects.filter(team=self.team, name="Over Limit Meter").exists())
        self.assertFalse(DeviceTemplate.objects.filter(created_by_team=self.team, model_number="LIMIT").exists())
        validation.assert_not_called()

    @patch("apps.devices.deployment_setup.start_validation")
    def test_manual_setup_rejects_gateway_loopback_target(self, validation):
        self._enable_guided_setup()

        response = self.client.post(
            self.url,
            {
                "action": "manual",
                "manual_name": "Unsafe local target",
                "manual_protocol": "modbus_tcp",
                "manual_host": "127.0.0.1",
                "manual_port": "502",
                "point_key_1": "voltage",
                "point_address_1": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Device.objects.filter(team=self.team, name="Unsafe local target").exists())
        validation.assert_not_called()

    def test_gateway_status_poll_uses_heartbeat_freshness(self):
        self.gateway.status = "online"
        self.gateway.last_seen = timezone.now() - timezone.timedelta(minutes=10)
        self.gateway.save(update_fields=["status", "last_seen"])

        response = self.client.get(reverse("web_team:onboarding:gateway_status_poll", args=[self.team.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Waiting for the Gateway to connect")
        self.assertNotContains(response, "Gateway is online")

    def test_legacy_gateway_can_use_saved_discovery_with_verified_template(self):
        template = DeviceTemplate.objects.create(
            name="Verified Legacy Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={
                "voltage": {
                    "address": 1,
                    "functionCode": 3,
                    "type": "uint16",
                }
            },
            is_verified=True,
        )
        self.gateway.discovery_data = {
            "status": "complete",
            "devices": [
                {
                    "interface": "192.168.1.50:502",
                    "connection": "modbus_tcp",
                    "slave_id": 1,
                    "signature": "Legacy meter",
                }
            ],
        }
        self.gateway.save(update_fields=["discovery_data"])

        response = self.client.post(
            self.url,
            {
                "action": "validate_selected",
                "device_index": ["0"],
                "name_0": "Legacy meter",
                "template_0": str(template.pk),
            },
        )

        self.assertEqual(response.status_code, 302)
        item = DeploymentSetupItem.objects.get(run__gateway=self.gateway)
        self.assertEqual(item.state, "validated")
        self.assertEqual(item.trust_level, "novena_verified")
        self.assertFalse(RemoteCommand.objects.filter(gateway=self.gateway).exists())

        page = self.client.get(self.url)
        self.assertContains(page, 'data-equipment-row="configured"')
        self.assertContains(page, "1 of 1 validated")
        self.assertContains(page, 'aria-label="Validation complete"')
        self.assertContains(page, "All listed equipment is ready for the next step")
        self.assertContains(page, "Deploy and continue")

    def test_template_request_has_support_reference(self):
        response = self.client.post(
            self.url,
            {
                "action": "request_template",
                "request_manufacturer": "UnknownCo",
                "request_model": "X1",
                "request_protocol": "modbus_tcp",
            },
        )

        self.assertEqual(response.status_code, 302)
        request_row = EquipmentTemplateRequest.objects.get(team=self.team)
        self.assertTrue(request_row.support_reference)
