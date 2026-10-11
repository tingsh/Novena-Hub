"""Opt-in real broker + Hub lifecycle + Gateway runtime test; disposable state only.

NOVENA_LIVE_MQTT_TESTS=1 NOVENA_MQTT_TEST_ROOT=/var/lib/mosquitto/novena-tests
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import skipUnless

import paho.mqtt.client as mqtt
from django.test import TestCase, override_settings

from apps.devices.activation import decrypt_activation_secret, provision_gateway_activation
from apps.devices.gateway_release import dispatch_gateway_release, request_gateway_release
from apps.devices.models import GatewayInventory, Site
from apps.devices.mqtt_provisioning import _publish_dynsec_command
from apps.devices.services import claim_gateway_for_team, compute_claim_code
from apps.teams.models import Team

ROOT = Path(__file__).resolve().parents[3]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@skipUnless(os.environ.get("NOVENA_LIVE_MQTT_TESTS") == "1", "Opt in to disposable real-Mosquitto tests")
@override_settings(
    MQTT_PROVISIONING_REQUIRED=True,
    GATEWAY_ACTIVATION_ENCRYPTION_KEY="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class LocalDynsecLifecycleTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("NOVENA_MQTT_TEST_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.admin_port, self.edge_port, self.internal_port = free_port(), free_port(), free_port()
        self.state = self.directory / "dynamic-security.json"
        subprocess.run(
            ["mosquitto_ctrl", "dynsec", "init", str(self.state), "test-admin"],
            input="disposable-test-password\ndisposable-test-password\n",
            capture_output=True,
            text=True,
            check=True,
        )
        state = json.loads(self.state.read_text())
        state["defaultACLAccess"] = dict(
            publishClientSend=False, publishClientReceive=False, subscribe=False, unsubscribe=True
        )
        state["roles"].append({"rolename": "gateway", "acls": []})
        self.state.write_text(json.dumps(state))
        config = (ROOT / "deploy/mosquitto/local/novena-local-replay.conf").read_text()
        config = config.replace("@LAN_IP@", "127.0.0.1").replace("1883", str(self.edge_port))
        config = config.replace("1884", str(self.admin_port)).replace("1885", str(self.internal_port))
        config = config.replace("/var/lib/mosquitto/novena-dynsec/dynamic-security.json", str(self.state))
        path = self.directory / "broker.conf"
        path.write_text(config)
        self.log = (self.directory / "broker.log").open("w+")
        self.addCleanup(self.log.close)
        self.broker = subprocess.Popen(["mosquitto", "-c", str(path)], stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop_broker)
        self.settings_override = override_settings(
            MQTT_BROKER_HOST="127.0.0.1",
            MQTT_DYNSEC_PORT=self.admin_port,
            MQTT_DYNSEC_ADMIN_USER="test-admin",
            MQTT_DYNSEC_ADMIN_PASS="disposable-test-password",
            MQTT_DYNSEC_RESPONSE_TIMEOUT_SECONDS=2,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        for _ in range(30):
            if self.broker.poll() is not None:
                self.fail("Disposable Mosquitto failed to start; check AppArmor permissions for NOVENA_MQTT_TEST_ROOT.")
            try:
                with socket.create_connection(("127.0.0.1", self.admin_port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        _publish_dynsec_command({"command": "getRole", "rolename": "gateway"})
        self.team = Team.objects.create(name="Credential test", slug="credential-test")
        self.site = Site.objects.create(team=self.team, name="Test bench")
        self.serial = "NOV-DYNSEC-TEST"
        GatewayInventory.objects.create(serial_number=self.serial)

    def stop_broker(self):
        if self.broker.poll() is None:
            self.broker.terminate()
            try:
                self.broker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.broker.kill()
                self.broker.wait(timeout=5)

    def auth(self, username=None, password=None, port=None):
        event, codes = threading.Event(), []
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if username:
            client.username_pw_set(username, password)

        def connected(client, userdata, flags, code, properties):
            codes.append(code.value)
            event.set()

        client.on_connect = connected
        try:
            client.connect("127.0.0.1", port or self.edge_port)
            client.loop_start()
            self.assertTrue(event.wait(3), "No CONNACK: cannot count network failure as credential rejection")
            return codes[0]
        finally:
            client.disconnect()
            client.loop_stop()

    def publish_ack(self, username, password, topic):
        event, codes = threading.Event(), []
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        client.username_pw_set(username, password)

        def connected(client, userdata, flags, code, properties):
            if code.value != 0:
                codes.append(code.value)
                event.set()
            else:
                client.publish(topic, "{}", qos=1)

        def published(client, userdata, mid, code, properties):
            codes.append(code.value)
            event.set()

        client.on_connect, client.on_publish = connected, published
        try:
            client.connect("127.0.0.1", self.edge_port)
            client.loop_start()
            self.assertTrue(event.wait(3), "No PUBACK received")
            return codes[0]
        finally:
            client.disconnect()
            client.loop_stop()

    def test_provisioning_retry_preserves_role_assignment(self):
        gateway, _ = self.claim()
        activation = gateway.activations.latest("generation")
        self.assertTrue(provision_gateway_activation(activation.pk))
        password = decrypt_activation_secret(activation.encrypted_mqtt_password)
        self.assertEqual(self.auth(self.serial, password), 0)

    def claim(self):
        gateway = claim_gateway_for_team(
            self.team, self.site, "Credential test", self.serial, compute_claim_code(self.serial)
        )
        activation = gateway.activations.get()
        password = decrypt_activation_secret(activation.encrypted_mqtt_password)
        self.assertTrue(provision_gateway_activation(activation.pk))
        return gateway, password

    def test_claim_runtime_reconnect_release_reject_and_reclaim(self):
        self.assertIn(self.auth(), (134, 135))
        self.assertEqual(self.auth(port=self.internal_port), 0)
        gateway, password = self.claim()
        self.assertEqual(self.auth(self.serial, password), 0)
        self.assertEqual(self.auth(self.serial, password, port=self.admin_port), 0)
        self.assertIn(self.publish_ack(self.serial, password, f"v1/gateway/{self.serial}/telemetry"), (0, 16))
        self.assertEqual(self.publish_ack(self.serial, password, "v1/gateway/OTHER/telemetry"), 135)
        self.assertEqual(self.publish_ack(self.serial, password, "v1/gateway/telemetry"), 135)
        self.assertEqual(self.auth("bootstrap:" + self.serial, compute_claim_code(self.serial)), 0)
        # Exercise the real Gateway client, with SQLite state confined to this test.
        gateway_root = Path(os.environ.get("NOVENA_GATEWAY_ROOT", ROOT.parent / "Novena-Gateway"))
        sys.path.insert(0, str(gateway_root))
        from novena_gateway.gateway.novena_mqtt_publisher import NovenaMqttPublisher

        publisher = NovenaMqttPublisher(
            {
                "host": "127.0.0.1",
                "port": self.edge_port,
                "username": self.serial,
                "password": password,
                "storage": {"data_file_path": str(self.directory / "sqlite") + "/"},
            },
            serial_number=self.serial,
        )
        self.addCleanup(publisher.disconnect)
        publisher.connect()
        self.wait_connected(publisher)
        publisher._reconnect_with_current_credentials()
        self.wait_connected(publisher)
        release = request_gateway_release(gateway)
        dispatch_gateway_release(release.pk)
        release.refresh_from_db()
        self.assertEqual(release.status, "completed")
        self.assertIn(self.auth(self.serial, password), (134, 135))
        self.assertIn(self.auth("bootstrap:" + self.serial, compute_claim_code(self.serial)), (134, 135))
        fresh, fresh_password = self.claim()
        self.assertNotEqual(fresh.pk, gateway.pk)
        self.assertNotEqual(fresh_password, password)
        self.assertEqual(self.auth(self.serial, fresh_password), 0)
        self.assertIn(self.auth(self.serial, password), (134, 135))

    def wait_connected(self, publisher):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if publisher._client.is_connected():
                return
            time.sleep(0.05)
        self.fail("Gateway runtime did not reconnect with its provisioned credentials")

    def test_missing_admin_response_prevents_release(self):
        gateway, _ = self.claim()
        release = request_gateway_release(gateway)
        # Rejected admin authentication must not produce a completed release.
        with override_settings(MQTT_DYNSEC_ADMIN_PASS="incorrect-test-password"):
            dispatch_gateway_release(release.pk)
        release.refresh_from_db()
        self.assertEqual(release.status, "retry")
        self.assertEqual(GatewayInventory.objects.get(serial_number=self.serial).status, "claimed")
