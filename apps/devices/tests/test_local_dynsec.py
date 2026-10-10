"""Local configuration and fail-closed release regression tests (no live broker)."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.devices.gateway_release import dispatch_gateway_release, request_gateway_release
from apps.devices.mqtt_provisioning import DynsecCommandError, _response_matches
from apps.devices.tests.test_managed_gateway_hardening import ManagedGatewayFixture

spec = importlib.util.spec_from_file_location(
    "local_dynsec_setup", Path(__file__).resolve().parents[3] / "scripts/hardware-test/local_dynsec.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class LocalStateValidationTest(SimpleTestCase):
    def state(self):
        return {
            "roles": [{"rolename": "gateway", "acls": []}],
            "clients": [],
            "defaultACLAccess": {"publishClientSend": False, "publishClientReceive": False, "subscribe": False},
        }

    def test_legacy_migration_state_rejected_without_modification(self):
        state = {"roles": [{"roleName": "gateway"}]}
        with self.assertRaisesRegex(ValueError, "Legacy"):
            setup.validate_state(state)
        self.assertEqual(state, {"roles": [{"roleName": "gateway"}]})

    def test_empty_shared_gateway_role_is_valid(self):
        setup.validate_state(self.state())

    def test_shared_publish_permission_rejected(self):
        state = self.state()
        state["roles"][0]["acls"] = [{"topic": "#", "allow": True}]
        with self.assertRaisesRegex(ValueError, "Shared gateway"):
            setup.validate_state(state)

    def test_cross_serial_role_rejected(self):
        state = self.state()
        state["roles"].append({"rolename": "gw-A", "acls": [{"topic": "v1/gateway/B/#", "allow": True}]})
        with self.assertRaisesRegex(ValueError, "outside"):
            setup.validate_state(state)

    def test_unrelated_command_response_cannot_confirm_revocation(self):
        command = {"command": "deleteClient", "correlationData": "this-operation"}
        self.assertFalse(
            _response_matches(command, {"command": "deleteClient", "correlationData": "another-operation"})
        )
        self.assertFalse(_response_matches(command, {"command": "disableClient", "correlationData": "this-operation"}))
        self.assertTrue(_response_matches(command, command))


class PartialRevocationTest(ManagedGatewayFixture):
    def test_each_partial_failure_quarantines_then_verified_retry_completes(self):
        from django.test import override_settings

        for failed_step in range(6):
            with self.subTest(failed_step=failed_step), override_settings(MQTT_PROVISIONING_REQUIRED=True, DEBUG=True):
                # Roll back each scenario so all six revocation commands are checked.
                from django.db import transaction

                with transaction.atomic():
                    release = request_gateway_release(self.gateway)
                    with patch(
                        "apps.devices.mqtt_provisioning._publish_dynsec_command",
                        side_effect=[{}] * failed_step + [DynsecCommandError("denied")],
                    ):
                        dispatch_gateway_release(release.pk)
                    release.refresh_from_db()
                    self.inventory.refresh_from_db()
                    self.gateway.refresh_from_db()
                    self.assertEqual(release.status, "retry")
                    self.assertEqual(self.inventory.status, "claimed")
                    self.assertEqual(self.gateway.lifecycle_status, "release_pending")
                    release.next_attempt_at = timezone.now()
                    release.save()
                    with patch("apps.devices.mqtt_provisioning._publish_dynsec_command", return_value={}):
                        dispatch_gateway_release(release.pk)
                    release.refresh_from_db()
                    self.assertEqual(release.status, "completed")
                    transaction.set_rollback(True)
                self.gateway.refresh_from_db()
                self.inventory.refresh_from_db()


class DynsecResponseVerificationTest(SimpleTestCase):
    def test_puback_alone_or_wrong_response_never_confirms_revocation(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from django.test import override_settings

        from apps.devices.mqtt_provisioning import _publish_dynsec_command

        for mode in ("no-response", "wrong-correlation", "rejected", "verified"):
            with (
                self.subTest(mode=mode),
                override_settings(
                    MQTT_DYNSEC_RESPONSE_TIMEOUT_SECONDS=0.01,
                    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
                ),
            ):
                client = MagicMock()
                client.connect.side_effect = lambda *args, client=client: client.on_connect(client, None, None, 0, None)

                def publish(topic, payload, qos, mode=mode, client=client):
                    response = json.loads(payload)["commands"][0]
                    if mode == "wrong-correlation":
                        response["correlationData"] = "another-command"
                    if mode == "rejected":
                        response["error"] = "Permission denied"
                    if mode != "no-response":
                        client.on_message(
                            client, None, SimpleNamespace(payload=json.dumps({"responses": [response]}).encode())
                        )
                    result = MagicMock()
                    result.rc = 0
                    result.is_published.return_value = True
                    return result

                client.publish.side_effect = publish
                with patch("apps.devices.mqtt_provisioning.mqtt.Client", return_value=client):
                    if mode == "verified":
                        self.assertEqual(
                            _publish_dynsec_command({"command": "deleteClient", "username": "test"})["command"],
                            "deleteClient",
                        )
                    else:
                        with self.assertRaises(RuntimeError):
                            _publish_dynsec_command({"command": "deleteClient", "username": "test"})
                client.disconnect.assert_called_once()
