import json

from django.test import TestCase
from django.utils import timezone

from apps.devices.models import (
    CommandOutbox,
    Device,
    DeviceCommand,
    DeviceTemplate,
    Gateway,
    RemoteCommand,
    RpcCommand,
    Site,
)
from apps.devices.remote_control import CommandDenied
from apps.devices.services import process_command_response, send_device_command
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser


class DeviceCommandTest(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create(email="test@example.com", username="testuser")
        self.team = Team.objects.create(name="Test Team", slug="test-team")
        self.site = Site.objects.create(team=self.team, name="Main Plant")
        Membership.objects.create(team=self.team, user=self.user, role="manager")
        self.gateway = Gateway.objects.create(
            team=self.team, site=self.site, name="GW-001", serial_number="SN123", access_token="token123"
        )
        self.template = DeviceTemplate.objects.create(
            name="Verified VFD",
            device_type="vfd",
            protocol="modbus_tcp",
            is_verified=True,
            register_map={
                "speed_setpoint": {
                    "address": 100,
                    "functionCode": 6,
                    "type": "16uint",
                    "unit": "RPM",
                    "min": 0,
                    "max": 1500,
                    "writable": True,
                },
                "motor_speed": {
                    "address": 101,
                    "functionCode": 3,
                    "type": "16uint",
                    "unit": "RPM",
                },
            },
        )
        self.device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Motor 1",
            device_type="vfd",
            protocol="modbus_tcp",
            template=self.template,
        )

    def test_write_is_monitoring_only_by_default_and_denial_is_audited(self):
        with self.assertRaises(CommandDenied) as raised:
            send_device_command(self.device, self.user, "speed_setpoint", 1200)

        self.assertEqual(raised.exception.code, "monitoring_only")
        governed = RemoteCommand.objects.get()
        self.assertEqual(governed.status, RemoteCommand.Status.POLICY_DENIED)
        self.assertEqual(governed.events.get().event_type, "policy_denied")
        self.assertFalse(CommandOutbox.objects.exists())
        self.assertFalse(DeviceCommand.objects.exists())

    def test_send_read_command_uses_exact_mapping_and_transactional_outbox(self):
        command = send_device_command(
            self.device,
            self.user,
            "motor_speed",
            command_type="read",
        )

        self.assertEqual(command.status, "pending")
        self.assertEqual(command.command_type, "read")
        self.assertIsNone(command.value)
        governed = command.remote_command
        self.assertEqual(governed.status, RemoteCommand.Status.QUEUED)
        self.assertTrue(CommandOutbox.objects.filter(command=governed, status="pending").exists())
        params = governed.request_payload["params"]
        self.assertEqual(params["device_id"], str(self.device.pk))
        self.assertEqual(params["device_name"], self.device.name)
        self.assertEqual(params["functionCode"], 3)
        self.assertEqual(params["address"], 101)

    def test_float32_read_requests_two_modbus_registers(self):
        from apps.devices.remote_control import resolve_device_params

        register_map = dict(self.template.register_map)
        register_map["temperature"] = {"address": 3000, "functionCode": 3, "type": "float32"}
        self.template.register_map = register_map
        self.template.save(update_fields=["register_map"])

        command = send_device_command(self.device, self.user, "temperature", command_type="read")
        self.assertEqual(command.remote_command.request_payload["params"]["objectsCount"], 2)
        params, _ = resolve_device_params(self.device, "temperature", "read_device")
        self.assertEqual(params["objectsCount"], 2)

    def test_raw_register_parameters_and_unmapped_keys_are_rejected(self):
        with self.assertRaisesMessage(ValueError, "Raw register parameters"):
            send_device_command(
                self.device,
                self.user,
                "motor_speed",
                command_type="read",
                params={"functionCode": 3, "address": 0},
            )
        with self.assertRaises(CommandDenied) as raised:
            send_device_command(self.device, self.user, "missing_key", command_type="read")
        self.assertEqual(raised.exception.code, "unmapped_command_key")

    def test_process_command_response_success(self):
        # Setup a pending command
        command = DeviceCommand.objects.create(
            team=self.team,
            device=self.device,
            command_key="set_speed",
            value=50,
            transaction_id="test-tx-123",
            status="sent",
        )

        response_payload = json.dumps({"device": self.device.name, "id": "test-tx-123", "data": {"success": True}})

        process_command_response(response_payload)

        command.refresh_from_db()
        self.assertEqual(command.status, "executed")
        self.assertIsNotNone(command.executed_at)

    def test_process_current_rpc_response_updates_linked_device_command(self):
        rpc = RpcCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            request_id="12345678-1234-1234-1234-123456789014",
            method="write_device",
            params={"device_name": self.device.name},
        )
        command = DeviceCommand.objects.create(
            team=self.team,
            device=self.device,
            command_type="write",
            command_key="set_speed",
            value=50,
            transaction_id="test-tx-current",
            rpc_command=rpc,
            status="sent",
        )

        response_payload = json.dumps(
            {
                "request_id": str(rpc.request_id),
                "method": "write_device",
                "status": "success",
                "stage": "field_execution_verified",
                "result": {"operation": "write", "response": {"success": True}},
            }
        )

        process_command_response(response_payload)

        rpc.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(rpc.status, "success")
        self.assertEqual(command.status, "executed")
        self.assertEqual(command.response_payload["result"]["operation"], "write")

    def test_generic_success_without_allowlisted_stage_is_not_called_executed(self):
        rpc = RpcCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            request_id="12345678-1234-1234-1234-123456789099",
            method="write_device",
            params={"device_name": self.device.name},
        )
        command = DeviceCommand.objects.create(
            team=self.team,
            device=self.device,
            command_type="write",
            command_key="set_speed",
            value=50,
            transaction_id="test-tx-generic-success",
            rpc_command=rpc,
            status="sent",
        )

        process_command_response(
            json.dumps(
                {
                    "request_id": str(rpc.request_id),
                    "method": "write_device",
                    "status": "success",
                    "result": {"accepted": True},
                }
            )
        )

        command.refresh_from_db()
        self.assertEqual(command.status, "sent")
        self.assertIsNone(command.executed_at)

    def test_completed_read_updates_customer_command_status(self):
        rpc = RpcCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            request_id="12345678-1234-1234-1234-123456789098",
            method="read_device",
            params={"device_name": self.device.name},
        )
        command = DeviceCommand.objects.create(
            team=self.team,
            device=self.device,
            command_type="read",
            command_key="motor_speed",
            transaction_id="test-tx-read-completed",
            rpc_command=rpc,
            status="sent",
        )

        process_command_response(
            json.dumps(
                {
                    "request_id": str(rpc.request_id),
                    "method": "read_device",
                    "status": "success",
                    "stage": "diagnostic_completed",
                    "result": {"operation": "read", "read_value": 42, "device_accepted": True},
                }
            )
        )

        rpc.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(rpc.response_stage, "diagnostic_completed")
        self.assertEqual(command.status, "executed")
        self.assertEqual(command.response_payload["result"]["read_value"], 42)

    def test_process_command_response_failure(self):
        command = DeviceCommand.objects.create(
            team=self.team,
            device=self.device,
            command_key="set_speed",
            value=100,
            transaction_id="test-tx-456",
            status="sent",
        )

        response_payload = json.dumps(
            {"device": self.device.name, "id": "test-tx-456", "data": {"success": False, "error": "Hardware failure"}}
        )

        process_command_response(response_payload)

        command.refresh_from_db()
        self.assertEqual(command.status, "failed")
        self.assertEqual(command.error_message, "Hardware failure")

    def test_rpc_timeout_updates_linked_device_command(self):
        from datetime import timedelta

        from apps.devices.tasks import check_rpc_timeouts

        rpc = RpcCommand.objects.create(
            team=self.team,
            gateway=self.gateway,
            request_id="12345678-1234-1234-1234-123456789015",
            method="read_device",
            params={"device_name": self.device.name},
        )
        RpcCommand.objects.filter(pk=rpc.pk).update(sent_at=timezone.now() - timedelta(seconds=90))
        command = DeviceCommand.objects.create(
            team=self.team,
            device=self.device,
            command_type="read",
            command_key="manual_read",
            transaction_id="test-tx-timeout",
            rpc_command=rpc,
            status="sent",
        )

        self.assertEqual(check_rpc_timeouts(), 1)

        rpc.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(rpc.status, "timeout")
        self.assertEqual(command.status, "timed_out")
