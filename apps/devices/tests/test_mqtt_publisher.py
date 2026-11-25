from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from apps.telemetry import mqtt_publisher


@override_settings(
    MQTT_PUBLISHER_CLIENT_ID="novena-test-publisher",
    MQTT_BROKER_HOST="localhost",
    MQTT_BROKER_PORT=1883,
)
class MqttPublisherConnectionTest(SimpleTestCase):
    def setUp(self):
        self.client_patch = patch.object(mqtt_publisher, "_client", None)
        self.pid_patch = patch.object(mqtt_publisher, "_client_pid", None)
        self.event_patch = patch.object(mqtt_publisher, "_connected_event", None)
        self.client_patch.start()
        self.pid_patch.start()
        self.event_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.pid_patch.stop)
        self.addCleanup(self.event_patch.stop)

    @patch.object(mqtt_publisher, "os")
    @patch.object(mqtt_publisher.secrets, "token_hex", side_effect=["a1b2c3d4", "e5f6a7b8"])
    @patch.object(mqtt_publisher.mqtt, "Client")
    def test_each_worker_gets_a_distinct_connected_client(self, client_factory, _token, _pid):
        _pid.getpid.side_effect = [101, 102]
        first, second = Mock(), Mock()
        for client in (first, second):
            client.is_connected.return_value = True
            client.loop_start.side_effect = lambda client=client: client.on_connect(client, None, None, 0)
        client_factory.side_effect = [first, second]

        self.assertIs(mqtt_publisher.get_mqtt_client(), first)
        self.assertIs(mqtt_publisher.get_mqtt_client(), second)

        ids = [call.kwargs["client_id"] for call in client_factory.call_args_list]
        self.assertEqual(ids, ["novena-test-publisher-101-a1b2c3d4", "novena-test-publisher-102-e5f6a7b8"])

    @patch.object(mqtt_publisher, "os")
    @patch.object(mqtt_publisher.mqtt, "Client")
    def test_publish_waits_for_broker_connection(self, client_factory, _pid):
        _pid.getpid.return_value = 101
        client = client_factory.return_value
        client.is_connected.return_value = False
        with patch.object(mqtt_publisher.threading, "Event") as event_factory:
            event_factory.return_value.wait.return_value = False
            with self.assertRaisesMessage(RuntimeError, "not connected to the broker"):
                mqtt_publisher.get_mqtt_client()
        client.publish.assert_not_called()

    @patch.object(mqtt_publisher, "os")
    @patch.object(mqtt_publisher.mqtt, "Client")
    def test_disconnection_clears_connection_readiness(self, client_factory, _pid):
        _pid.getpid.return_value = 101
        client = client_factory.return_value
        client.is_connected.return_value = True
        client.loop_start.side_effect = lambda: client.on_connect(client, None, None, 0)
        mqtt_publisher.get_mqtt_client()

        client.on_disconnect(client, None, None, 1)
        self.assertFalse(mqtt_publisher._connected_event.is_set())
