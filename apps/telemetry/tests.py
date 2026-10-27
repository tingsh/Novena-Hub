import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.http import Http404
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.devices.models import (
    Device,
    DeviceTemplate,
    Gateway,
    GatewayActivation,
    GatewayConfig,
    GatewayInventory,
    RpcCommand,
    Site,
)
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_OWNER
from apps.telemetry.claim_identity import gateway_claim_id
from apps.telemetry.consumers import TelemetryConsumer
from apps.telemetry.management.commands.mqtt_consumer import Command as MqttConsumerCommand
from apps.telemetry.models import GatewayLog, TelemetryData
from apps.telemetry.services import ingest_telemetry_data
from apps.telemetry.tasks import flush_logs_buffer_task, flush_telemetry_buffer_task
from apps.users.models import CustomUser


class MqttMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = json.dumps(payload).encode("utf-8")


def claim_gateway(gateway):
    GatewayInventory.objects.create(
        serial_number=gateway.serial_number,
        status="claimed",
        gateway=gateway,
        claimed_by_team=gateway.team,
    )


def buffered_gateway_payload(gateway, payload):
    return {
        **payload,
        "_topic_gateway_sn": gateway.serial_number,
        "_topic_gateway_id": gateway.pk,
    }


@pytest.mark.django_db
def test_reclaimed_gateway_rejects_unscoped_and_previous_claim_data():
    team = Team.objects.create(name="New owner", slug="new-owner")
    site = Site.objects.create(team=team, name="Factory")
    gateway = Gateway.objects.create(
        team=team, site=site, name="Gateway", serial_number="GW-RECLAIM", access_token="token"
    )
    claim_gateway(gateway)
    GatewayActivation.objects.create(
        team=team, gateway=gateway, expires_at=timezone.now(), status="acknowledged"
    )
    command = MqttConsumerCommand()
    topic = "v1/gateway/GW-RECLAIM/logs"
    payload = {"serial_number": gateway.serial_number, "logs": []}

    assert command._resolve_inbound_gateway(topic, payload) == (None, None)
    assert command._resolve_inbound_gateway(topic, {**payload, "claim_id": "old-claim"}) == (None, None)
    assert command._resolve_inbound_gateway(
        topic, {**payload, "claim_id": gateway_claim_id(gateway)}
    ) == ("logs", gateway)


def test_websocket_connection_uses_effective_plan_and_device_interval():
    user = MagicMock(is_authenticated=True, email="operator@example.com")
    user.teams.filter.return_value.exists.return_value = True
    device = MagicMock(id=42, team_id=7)
    consumer = TelemetryConsumer()
    consumer.scope = {"url_route": {"kwargs": {"device_id": 42}}, "user": user}
    consumer.channel_name = "test-channel"
    consumer.channel_layer = MagicMock()
    consumer.channel_layer.group_add = AsyncMock()
    consumer.accept = AsyncMock()
    consumer.send_json = AsyncMock()

    with (
        patch("apps.telemetry.consumers.Device.objects.select_related") as select_related,
        patch("apps.telemetry.consumers.get_effective_polling_interval_seconds", return_value=2.0) as effective,
    ):
        select_related.return_value.get.return_value = device
        async_to_sync(consumer.connect)()

    effective.assert_called_once_with(device)
    consumer.send_json.assert_awaited_once_with({"type": "connection_established", "min_interval": 2.0})


@pytest.mark.django_db
def test_gateway_attribute_ingest_persists_edge_diagnostics():
    team = Team.objects.create(name="Gateway Diagnostics", slug="gateway-diagnostics")
    site = Site.objects.create(team=team, name="Factory")
    gateway = Gateway.objects.create(
        team=team,
        site=site,
        name="GW-DIAG",
        serial_number="GW-DIAG-001",
        access_token="diag-token",
    )
    claim_gateway(gateway)
    config_record = GatewayConfig.objects.create(
        team=team,
        gateway=gateway,
        config_json={"connectors": []},
        request_id="11111111-1111-1111-1111-111111111111",
        revision=1,
        checksum="diagnostic-checksum",
    )

    command = MqttConsumerCommand()
    command._handle_attributes(
        {
            "serial_number": "GW-DIAG-001",
            "attributes": {
                "status": "online",
                "remote_control_protocol_version": 1,
                "remote_control_capabilities": [
                    "governed_commands_v1",
                    "local_writeback_v1",
                    "lifecycle_stages_v1",
                    "idempotent_replay_v1",
                ],
                "remote_control_local_writeback_enabled": True,
                "internet_reachable": False,
                "dns_ok": True,
                "broker_tcp_ok": False,
                "broker_tcp_error": "timed out",
                "tls_ok": False,
                "mqtt_connected": False,
                "mqtt_last_error": "unexpected_disconnect_rc_7",
                "device_health": {"Power Meter 1": {"poll_status": "degraded"}},
                "ota_status": "rolled_back",
                "ota_version": "1.2.0",
                "ota_error": "health check failed",
                "ota_rollback_performed": True,
                "config_update_request_id": str(config_record.request_id),
                "config_revision": config_record.revision,
                "config_checksum": config_record.checksum,
                "config_idempotency_key": str(config_record.idempotency_key),
                "config_update_status": "rolled_back",
                "config_update_error": "Broken Modbus",
                "rollback_performed": True,
                "connector_results": [{"name": "Broken Modbus", "status": "error"}],
            },
        }
    )

    gateway.refresh_from_db()
    config_record.refresh_from_db()

    assert gateway.internet_reachable is False
    assert gateway.remote_control_protocol_version == 1
    assert "local_writeback_v1" in gateway.remote_control_capabilities
    assert gateway.remote_control_local_writeback_enabled is True
    assert gateway.dns_ok is True
    assert gateway.broker_tcp_ok is False
    assert gateway.broker_tcp_error == "timed out"
    assert gateway.device_health["Power Meter 1"]["poll_status"] == "degraded"
    assert gateway.ota_status == "rolled_back"
    assert gateway.ota_rollback_performed is True
    assert config_record.status == "rolled_back"
    assert config_record.rollback_performed is True
    assert config_record.connector_results[0]["name"] == "Broken Modbus"


@pytest.mark.django_db
def test_gateway_attribute_ingest_normalizes_null_edge_diagnostic_strings():
    team = Team.objects.create(name="Gateway Null Diagnostics", slug="gateway-null-diagnostics")
    site = Site.objects.create(team=team, name="Factory")
    gateway = Gateway.objects.create(
        team=team,
        site=site,
        name="GW-NULL-DIAG",
        serial_number="GW-NULL-DIAG-001",
        access_token="diag-null-token",
    )
    claim_gateway(gateway)

    command = MqttConsumerCommand()
    command._handle_attributes(
        {
            "serial_number": "GW-NULL-DIAG-001",
            "attributes": {
                "status": "online",
                "broker_host": "192.168.100.7",
                "broker_port": 1883,
                "broker_tcp_ok": True,
                "broker_tcp_error": None,
                "dns_error": None,
                "tls_error": None,
                "mqtt_connected": True,
                "mqtt_last_error": None,
                "ota_error": None,
            },
        }
    )

    gateway.refresh_from_db()

    assert gateway.status == "online"
    assert gateway.lifecycle_status == "online"
    assert gateway.broker_tcp_error == ""
    assert gateway.dns_error == ""
    assert gateway.tls_error == ""
    assert gateway.mqtt_last_error == ""
    assert gateway.ota_error == ""


@pytest.mark.django_db
def test_scoped_mqtt_rejects_payload_topic_serial_mismatch(caplog):
    team_a = Team.objects.create(name="Tenant A", slug="tenant-a")
    team_b = Team.objects.create(name="Tenant B", slug="tenant-b")
    site_a = Site.objects.create(team=team_a, name="Site A")
    site_b = Site.objects.create(team=team_b, name="Site B")
    gateway_a = Gateway.objects.create(team=team_a, site=site_a, name="GW-A", serial_number="GW-A", access_token="a")
    gateway_b = Gateway.objects.create(team=team_b, site=site_b, name="GW-B", serial_number="GW-B", access_token="b")
    claim_gateway(gateway_a)

    command = MqttConsumerCommand()
    command.redis_client = MagicMock()
    message = MqttMessage(
        f"v1/gateway/{gateway_a.serial_number}/telemetry",
        {"serial_number": gateway_b.serial_number, "values": {"temperature": 99.0}},
    )

    with caplog.at_level("WARNING", logger="novena_hub"):
        command.on_message(None, None, message)

    command.redis_client.rpush.assert_not_called()
    assert "Rejected MQTT telemetry" in caplog.text


@pytest.mark.django_db
def test_scoped_mqtt_queues_trusted_topic_gateway_identity():
    team = Team.objects.create(name="Scoped Queue", slug="scoped-queue")
    site = Site.objects.create(team=team, name="Factory")
    gateway = Gateway.objects.create(team=team, site=site, name="GW", serial_number="GW-SCOPED", access_token="tok")
    claim_gateway(gateway)

    command = MqttConsumerCommand()
    command.redis_client = MagicMock()
    message = MqttMessage(
        f"v1/gateway/{gateway.serial_number}/telemetry",
        {"values": {"device_name": "Power Meter", "active_power": 55.0}},
    )

    command.on_message(None, None, message)

    _, queued = command.redis_client.rpush.call_args.args
    queued_payload = json.loads(queued)
    assert queued_payload["_topic_gateway_sn"] == gateway.serial_number


@pytest.mark.django_db
def test_flush_telemetry_uses_trusted_topic_gateway_over_payload_serial():
    team_a = Team.objects.create(name="Trusted A", slug="trusted-a")
    team_b = Team.objects.create(name="Trusted B", slug="trusted-b")
    site_a = Site.objects.create(team=team_a, name="Site A")
    site_b = Site.objects.create(team=team_b, name="Site B")
    gateway_a = Gateway.objects.create(
        team=team_a, site=site_a, name="GW-A", serial_number="GW-TRUST-A", access_token="a"
    )
    gateway_b = Gateway.objects.create(
        team=team_b, site=site_b, name="GW-B", serial_number="GW-TRUST-B", access_token="b"
    )
    claim_gateway(gateway_a)
    device_a = Device.objects.create(
        team=team_a,
        site=site_a,
        gateway=gateway_a,
        name="Meter A",
        device_type="power_meter",
        protocol="modbus_tcp",
    )
    device_b = Device.objects.create(
        team=team_b,
        site=site_b,
        gateway=gateway_b,
        name="Meter B",
        device_type="power_meter",
        protocol="modbus_tcp",
    )
    payload = buffered_gateway_payload(
        gateway_a,
        {
            "serial_number": gateway_b.serial_number,
            "device_id": device_a.id,
            "values": {"active_power": 101.0},
        },
    )
    raw_payload = json.dumps(payload).encode("utf-8")
    mock_redis = MagicMock()
    mock_pipeline = MagicMock()
    mock_redis.pipeline.return_value.__enter__.return_value = mock_pipeline
    mock_pipeline.execute.return_value = [[raw_payload], 1]

    with patch("redis.Redis.from_url", return_value=mock_redis):
        flush_telemetry_buffer_task()

    assert TelemetryData.objects.filter(device=device_a, key="active_power").exists()
    assert not TelemetryData.objects.filter(device=device_b, key="active_power").exists()


@pytest.mark.django_db
def test_flush_logs_uses_trusted_topic_gateway_over_payload_serial():
    team_a = Team.objects.create(name="Logs A", slug="logs-a")
    team_b = Team.objects.create(name="Logs B", slug="logs-b")
    site_a = Site.objects.create(team=team_a, name="Site A")
    site_b = Site.objects.create(team=team_b, name="Site B")
    gateway_a = Gateway.objects.create(
        team=team_a, site=site_a, name="GW-A", serial_number="GW-LOG-A", access_token="a"
    )
    gateway_b = Gateway.objects.create(
        team=team_b, site=site_b, name="GW-B", serial_number="GW-LOG-B", access_token="b"
    )
    claim_gateway(gateway_a)
    payload = buffered_gateway_payload(
        gateway_a,
        {
            "serial_number": gateway_b.serial_number,
            "logs": [{"ts": 1714000000000, "level": "INFO", "logger": "test", "message": "hello"}],
        },
    )
    raw_payload = json.dumps(payload).encode("utf-8")
    mock_redis = MagicMock()
    mock_pipeline = MagicMock()
    mock_redis.pipeline.return_value.__enter__.return_value = mock_pipeline
    mock_pipeline.execute.return_value = [[raw_payload], 1]

    with patch("redis.Redis.from_url", return_value=mock_redis):
        flush_logs_buffer_task()

    assert GatewayLog.objects.filter(gateway=gateway_a, message="hello").exists()
    assert not GatewayLog.objects.filter(gateway=gateway_b, message="hello").exists()


@pytest.mark.django_db
def test_scoped_attribute_mismatch_does_not_update_other_gateway():
    team_a = Team.objects.create(name="Attr A", slug="attr-a")
    team_b = Team.objects.create(name="Attr B", slug="attr-b")
    site_a = Site.objects.create(team=team_a, name="Site A")
    site_b = Site.objects.create(team=team_b, name="Site B")
    gateway_a = Gateway.objects.create(
        team=team_a, site=site_a, name="GW-A", serial_number="GW-ATTR-A", access_token="a"
    )
    gateway_b = Gateway.objects.create(
        team=team_b, site=site_b, name="GW-B", serial_number="GW-ATTR-B", access_token="b"
    )
    claim_gateway(gateway_a)
    command = MqttConsumerCommand()

    command.on_message(
        None,
        None,
        MqttMessage(
            f"v1/gateway/{gateway_a.serial_number}/attributes",
            {"serial_number": gateway_b.serial_number, "attributes": {"status": "online"}},
        ),
    )

    gateway_a.refresh_from_db()
    gateway_b.refresh_from_db()
    assert gateway_a.status == "offline"
    assert gateway_b.status == "offline"


@pytest.mark.django_db
def test_scoped_attribute_ack_cannot_update_another_gateway_config():
    team_a = Team.objects.create(name="Config A", slug="config-a")
    team_b = Team.objects.create(name="Config B", slug="config-b")
    site_a = Site.objects.create(team=team_a, name="Site A")
    site_b = Site.objects.create(team=team_b, name="Site B")
    gateway_a = Gateway.objects.create(
        team=team_a, site=site_a, name="GW-A", serial_number="GW-CFG-A", access_token="a"
    )
    gateway_b = Gateway.objects.create(
        team=team_b, site=site_b, name="GW-B", serial_number="GW-CFG-B", access_token="b"
    )
    claim_gateway(gateway_a)
    config_b = GatewayConfig.objects.create(
        team=team_b,
        gateway=gateway_b,
        config_json={"connectors": []},
        request_id="22222222-2222-2222-2222-222222222222",
    )
    command = MqttConsumerCommand()

    command.on_message(
        None,
        None,
        MqttMessage(
            f"v1/gateway/{gateway_a.serial_number}/attributes",
            {
                "serial_number": gateway_a.serial_number,
                "attributes": {
                    "config_update_request_id": str(config_b.request_id),
                    "config_update_status": "success",
                },
            },
        ),
    )

    config_b.refresh_from_db()
    assert config_b.status == "queued"


@pytest.mark.django_db
def test_scoped_rpc_response_cannot_complete_another_gateway_command():
    team_a = Team.objects.create(name="RPC A", slug="rpc-a")
    team_b = Team.objects.create(name="RPC B", slug="rpc-b")
    site_a = Site.objects.create(team=team_a, name="Site A")
    site_b = Site.objects.create(team=team_b, name="Site B")
    gateway_a = Gateway.objects.create(
        team=team_a, site=site_a, name="GW-A", serial_number="GW-RPC-A", access_token="a"
    )
    gateway_b = Gateway.objects.create(
        team=team_b, site=site_b, name="GW-B", serial_number="GW-RPC-B", access_token="b"
    )
    claim_gateway(gateway_a)
    rpc_b = RpcCommand.objects.create(
        team=team_b,
        gateway=gateway_b,
        request_id="33333333-3333-3333-3333-333333333333",
        method="ping",
        params={},
    )
    command = MqttConsumerCommand()

    command.on_message(
        None,
        None,
        MqttMessage(
            f"v1/gateway/{gateway_a.serial_number}/rpc/response",
            {
                "serial_number": gateway_a.serial_number,
                "request_id": str(rpc_b.request_id),
                "method": "ping",
                "status": "success",
                "result": {"pong": True},
            },
        ),
    )

    rpc_b.refresh_from_db()
    assert rpc_b.status == "pending"


@pytest.mark.django_db
def test_flush_telemetry_buffer_task():
    # 1. Create a Team, Site, Gateway, Device
    team = Team.objects.create(name="Telemetry Team", slug="telemetry-team")
    site = Site.objects.create(team=team, name="Telemetry Site")
    gateway = Gateway.objects.create(
        team=team, site=site, name="Telemetry Gateway", serial_number="GW-001", access_token="tok_123"
    )
    claim_gateway(gateway)
    device = Device.objects.create(
        team=team,
        site=site,
        gateway=gateway,
        name="Test Device",
        device_type="plc",
        protocol="modbus_tcp",
        status="online",
    )

    # 2. Prepare mock raw MQTT payload in Format A
    payload = buffered_gateway_payload(
        gateway,
        {"serial_number": "GW-001", "values": {"device_name": "Test Device", "active_power": 123.4}},
    )
    raw_payload = json.dumps(payload).encode("utf-8")

    # Mock Redis client and its pipeline
    mock_redis = MagicMock()
    mock_pipeline = MagicMock()
    mock_redis.pipeline.return_value.__enter__.return_value = mock_pipeline

    # pipe.execute() returns [ [list_of_payloads], delete_result ]
    mock_pipeline.execute.return_value = [[raw_payload], 1]

    with patch("redis.Redis.from_url", return_value=mock_redis):
        # 3. Call task
        flush_telemetry_buffer_task()

    # 4. Asserts
    assert TelemetryData.objects.filter(device=device, key="active_power").exists()
    data = TelemetryData.objects.get(device=device, key="active_power")
    assert data.value_numeric == 123.4


@pytest.mark.django_db
def test_direct_ingestion_keeps_legacy_fallback_for_single_device():
    team = Team.objects.create(name="Legacy Team", slug="legacy-team")
    site = Site.objects.create(team=team, name="Legacy Site")
    gateway = Gateway.objects.create(
        team=team,
        site=site,
        name="Legacy Gateway",
        serial_number="GW-LEGACY-ONE",
        access_token="legacy-one",
    )
    claim_gateway(gateway)
    device = Device.objects.create(
        team=team,
        site=site,
        gateway=gateway,
        name="Only Device",
        device_type="plc",
        protocol="modbus_tcp",
    )

    resolved = ingest_telemetry_data(gateway.serial_number, {"active_power": 42.0})

    assert resolved == device
    assert TelemetryData.objects.filter(device=device, key="active_power", value_numeric=42.0).exists()


@pytest.mark.django_db
def test_direct_ingestion_rejects_ambiguous_multi_device_identity(caplog):
    team = Team.objects.create(name="Ambiguous Team", slug="ambiguous-team")
    site = Site.objects.create(team=team, name="Ambiguous Site")
    gateway = Gateway.objects.create(
        team=team,
        site=site,
        name="Ambiguous Gateway",
        serial_number="GW-AMBIGUOUS",
        access_token="ambiguous",
    )
    claim_gateway(gateway)
    for name in ("Meter A", "Meter B"):
        Device.objects.create(
            team=team,
            site=site,
            gateway=gateway,
            name=name,
            device_type="power_meter",
            protocol="modbus_tcp",
        )

    resolved = ingest_telemetry_data(
        gateway.serial_number,
        {"active_power": 99.0},
        device_id="not-a-device",
    )

    assert resolved is None
    assert not TelemetryData.objects.filter(device__gateway=gateway).exists()
    assert "Rejected ambiguous telemetry" in caplog.text


@pytest.mark.django_db
def test_buffered_ingestion_rejects_ambiguous_multi_device_identity():
    team = Team.objects.create(name="Buffered Ambiguous", slug="buffered-ambiguous")
    site = Site.objects.create(team=team, name="Buffered Site")
    gateway = Gateway.objects.create(
        team=team,
        site=site,
        name="Buffered Gateway",
        serial_number="GW-BUFFERED-AMBIGUOUS",
        access_token="buffered-ambiguous",
    )
    claim_gateway(gateway)
    for name in ("Meter A", "Meter B"):
        Device.objects.create(
            team=team,
            site=site,
            gateway=gateway,
            name=name,
            device_type="power_meter",
            protocol="modbus_tcp",
        )
    raw_payload = json.dumps(
        buffered_gateway_payload(
            gateway,
            {
                "serial_number": gateway.serial_number,
                "values": {"active_power": 123.0},
            },
        )
    ).encode("utf-8")
    mock_redis = MagicMock()
    mock_pipeline = MagicMock()
    mock_redis.pipeline.return_value.__enter__.return_value = mock_pipeline
    mock_pipeline.execute.return_value = [[raw_payload], 1]

    with patch("redis.Redis.from_url", return_value=mock_redis):
        result = flush_telemetry_buffer_task()

    assert result == "Ingested 0 points."
    assert not TelemetryData.objects.filter(device__gateway=gateway).exists()


@pytest.mark.django_db
def test_device_telemetry_history_api_retention_limit():
    from django.test import RequestFactory

    from apps.telemetry.views import device_telemetry_history_api, get_retention_limit_days

    # 1. Create a Team (unsubscribed)
    team = Team.objects.create(name="Telemetry Team", slug="telemetry-team")
    site = Site.objects.create(team=team, name="Telemetry Site")
    device = Device.objects.create(
        team=team, site=site, name="Test Device", device_type="plc", protocol="modbus_tcp", status="online"
    )

    # Unsubscribed team gets 7 days retention limit by default
    assert get_retention_limit_days(team) == 7

    # 2. Test dynamic cap
    from apps.users.models import CustomUser

    user = CustomUser.objects.create_user(
        username="test_telemetry_user", email="test@telemetry.com", password="password123"
    )
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)

    factory = RequestFactory()
    # Request 30 days (720 hours) which exceeds the Starter 7-day limit (168 hours)
    request = factory.get(f"/a/{team.slug}/telemetry/api/history/{device.id}/?key=active_power&hours=720")
    request.team = team
    request.user = user

    # Call view
    response = device_telemetry_history_api(request, team_slug=team.slug, device_id=device.id)
    assert response.status_code == 200


@pytest.mark.django_db
def test_plan_history_window_changes_visibility_without_deleting_retained_rows():
    from django.test import RequestFactory

    from apps.subscriptions.metadata import ACTIVE_PRODUCTS
    from apps.subscriptions.tests.utils import create_subscription_for_team
    from apps.telemetry.views import device_telemetry_history_api

    team = Team.objects.create(name="Telemetry Upgrade Team", slug="telemetry-upgrade-team")
    site = Site.objects.create(team=team, name="Telemetry Site")
    device = Device.objects.create(
        team=team,
        site=site,
        name="Upgrade Meter",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="online",
    )
    user = CustomUser.objects.create_user(
        username="retention-window-user",
        email="retention-window@example.com",
        password="password123",
    )
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)

    old_point = TelemetryData.objects.create(
        device=device,
        key="active_power",
        value_numeric=20.0,
        timestamp=timezone.now() - timezone.timedelta(days=20),
    )
    recent_point = TelemetryData.objects.create(
        device=device,
        key="active_power",
        value_numeric=2.0,
        timestamp=timezone.now() - timezone.timedelta(days=2),
    )

    starter = next(product for product in ACTIVE_PRODUCTS if product.slug == "starter")
    business = next(product for product in ACTIVE_PRODUCTS if product.slug == "business")
    starter_subscription = create_subscription_for_team(team, starter)

    factory = RequestFactory()

    def visible_values():
        request = factory.get(f"/a/{team.slug}/telemetry/api/history/{device.id}/?key=active_power&hours=720")
        request.team = Team.objects.get(pk=team.pk)
        request.user = user
        response = device_telemetry_history_api(request, team_slug=team.slug, device_id=device.id)
        assert response.status_code == 200
        return json.loads(response.content)["values"]

    assert visible_values() == [2.0]

    create_subscription_for_team(team, business)
    assert visible_values() == [20.0, 2.0]

    team.subscription = starter_subscription
    team.save(update_fields=["subscription"])
    assert visible_values() == [2.0]
    assert TelemetryData.objects.filter(id__in=[old_point.id, recent_point.id]).count() == 2


def _sample_request(user, team, limit="20"):
    request = RequestFactory().get(f"/telemetry/samples/?limit={limit}")
    request.user = user
    request.team = team
    return request


@pytest.mark.django_db
def test_device_telemetry_samples_api_groups_template_columns():
    from apps.telemetry.views import device_telemetry_samples_api

    user = CustomUser.objects.create_user(
        username="samples_user",
        email="samples@example.com",
        password="password123",
    )
    team = Team.objects.create(name="Samples Team", slug="samples-team")
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="Samples Site")
    template = DeviceTemplate.objects.create(
        name="Power Meter Template",
        device_type="power_meter",
        protocol="modbus_tcp",
        register_map={
            "current": {"label": "Current", "unit": "A"},
            "voltage": {"label": "Voltage", "unit": "V"},
            "active_power": {"label": "Active Power", "unit": "W"},
            "run_command": {"label": "Run Command", "unit": "", "writable": True},
        },
    )
    device = Device.objects.create(
        team=team,
        site=site,
        template=template,
        name="Power Meter 1",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="online",
    )

    older = timezone.now() - timezone.timedelta(seconds=5)
    newer = timezone.now()
    TelemetryData.objects.bulk_create(
        [
            TelemetryData(device=device, timestamp=older, key="current", value_numeric=1.5),
            TelemetryData(device=device, timestamp=older, key="voltage", value_numeric=240.0),
            TelemetryData(device=device, timestamp=older, key="active_power", value_numeric=300.0),
            TelemetryData(device=device, timestamp=newer, key="current", value_numeric=2.2),
            TelemetryData(device=device, timestamp=newer, key="voltage", value_numeric=235.0),
            TelemetryData(device=device, timestamp=newer, key="active_power", value_numeric=295.0),
            TelemetryData(device=device, timestamp=newer, key="run_command", value_numeric=1.0),
        ]
    )

    response = device_telemetry_samples_api(_sample_request(user, team), team.slug, device.id)
    data = json.loads(response.content)

    assert response.status_code == 200
    assert data["limit"] == 20
    assert data["columns"] == [
        {"key": "current", "label": "Current", "unit": "A"},
        {"key": "voltage", "label": "Voltage", "unit": "V"},
        {"key": "active_power", "label": "Active Power", "unit": "W"},
    ]
    assert len(data["rows"]) == 2
    assert data["rows"][0]["timestamp"] == newer.isoformat()
    assert data["rows"][0]["timestamp_local"]
    assert data["timezone"] == "Asia/Singapore"
    assert data["rows"][0]["values"] == {"current": 2.2, "voltage": 235.0, "active_power": 295.0}
    assert "run_command" not in data["rows"][0]["values"]


@pytest.mark.django_db
def test_device_telemetry_samples_api_discovers_keys_and_clamps_limit():
    from apps.telemetry.views import device_telemetry_samples_api

    user = CustomUser.objects.create_user(
        username="manual_samples_user",
        email="manual-samples@example.com",
        password="password123",
    )
    team = Team.objects.create(name="Manual Samples Team", slug="manual-samples-team")
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="Manual Site")
    device = Device.objects.create(
        team=team,
        site=site,
        name="Manual Meter",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="online",
    )
    timestamp = timezone.now()
    TelemetryData.objects.bulk_create(
        [
            TelemetryData(device=device, timestamp=timestamp, key="temperature", value_numeric=28.0),
            TelemetryData(device=device, timestamp=timestamp, key="humidity", value_numeric=70.0),
        ]
    )

    response = device_telemetry_samples_api(_sample_request(user, team, limit="999"), team.slug, device.id)
    data = json.loads(response.content)

    assert response.status_code == 200
    assert data["limit"] == 50
    assert data["columns"] == [
        {"key": "humidity", "label": "Humidity", "unit": ""},
        {"key": "temperature", "label": "Temperature", "unit": ""},
    ]
    assert data["rows"][0]["values"] == {"humidity": 70.0, "temperature": 28.0}


@pytest.mark.django_db
def test_device_telemetry_samples_api_rejects_wrong_team_slug():
    from apps.telemetry.views import device_telemetry_samples_api

    user = CustomUser.objects.create_user(
        username="wrong_team_samples_user",
        email="wrong-team-samples@example.com",
        password="password123",
    )
    team = Team.objects.create(name="Right Team", slug="right-team")
    other_team = Team.objects.create(name="Other Team", slug="other-team")
    Membership.objects.create(team=other_team, user=user, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="Right Site")
    device = Device.objects.create(
        team=team,
        site=site,
        name="Protected Meter",
        device_type="power_meter",
        protocol="modbus_tcp",
    )

    with pytest.raises(Http404):
        device_telemetry_samples_api(_sample_request(user, other_team), other_team.slug, device.id)


@pytest.mark.django_db
def test_telemetry_surfaces_reject_authenticated_user_from_another_team():
    user = CustomUser.objects.create_user(
        username="tenant-a-operator",
        email="tenant-a-operator@example.com",
        password="password123",
    )
    tenant_a = Team.objects.create(name="Tenant A", slug="telemetry-tenant-a")
    tenant_b = Team.objects.create(name="Tenant B", slug="telemetry-tenant-b")
    Membership.objects.create(team=tenant_a, user=user, role=ROLE_OWNER)
    site_b = Site.objects.create(team=tenant_b, name="Private Site")
    device_b = Device.objects.create(
        team=tenant_b,
        site=site_b,
        name="Private Meter",
        device_type="power_meter",
        protocol="modbus_tcp",
    )
    TelemetryData.objects.create(
        device=device_b,
        timestamp=timezone.now(),
        key="active_power",
        value_numeric=123.0,
    )
    client = Client()
    client.force_login(user)

    protected_urls = [
        reverse("web_team:telemetry:chart_partial", args=[tenant_b.slug, device_b.id, "active_power"]),
        reverse("web_team:telemetry:kpi_partial", args=[tenant_b.slug, device_b.id, "active_power"]),
        reverse("web_team:telemetry:telemetry_analyzer", args=[tenant_b.slug, device_b.id]),
        reverse("web_team:telemetry:device_metrics_api", args=[tenant_b.slug, device_b.id]),
        reverse("web_team:telemetry:device_telemetry_history_api", args=[tenant_b.slug, device_b.id]),
        reverse("web_team:telemetry:device_telemetry_samples_api", args=[tenant_b.slug, device_b.id]),
        reverse("web_team:telemetry:export_telemetry_csv", args=[tenant_b.slug, device_b.id]),
        reverse("web_team:telemetry:site_profile_report", args=[tenant_b.slug, site_b.id]),
        reverse("web_team:telemetry:export_site_profile_report_csv", args=[tenant_b.slug, site_b.id]),
    ]

    for url in protected_urls:
        assert client.get(url).status_code == 404


@pytest.mark.django_db
def test_telemetry_history_api_returns_utc_and_site_local_labels():
    from apps.telemetry.views import device_telemetry_history_api
    from apps.utils.timezones import format_site_datetime

    user = CustomUser.objects.create_user(
        username="history_tz_user",
        email="history-tz@example.com",
        password="password123",
    )
    team = Team.objects.create(name="History TZ Team", slug="history-tz-team")
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="Jakarta Site", timezone="Asia/Jakarta")
    device = Device.objects.create(
        team=team,
        site=site,
        name="Jakarta Meter",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="online",
    )
    timestamp = timezone.now().replace(microsecond=0)
    TelemetryData.objects.create(device=device, timestamp=timestamp, key="temperature", value_numeric=28.0)

    request = RequestFactory().get(f"/a/{team.slug}/telemetry/api/history/{device.id}/?key=temperature&hours=24")
    request.user = user
    request.team = team
    response = device_telemetry_history_api(request, team.slug, device.id)
    data = json.loads(response.content)

    assert response.status_code == 200
    assert data["labels"] == [timestamp.isoformat()]
    assert data["labels_local"] == [format_site_datetime(timestamp, site, "%H:%M:%S")]
    assert data["timezone"] == "Asia/Jakarta"


@pytest.mark.django_db
def test_telemetry_history_api_preserves_boolean_values_for_status_widgets():
    from apps.telemetry.views import device_telemetry_history_api

    user = CustomUser.objects.create_user(
        username="history_bool_user",
        email="history-bool@example.com",
        password="password123",
    )
    team = Team.objects.create(name="History Bool Team", slug="history-bool-team")
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="Bool Site")
    device = Device.objects.create(
        team=team,
        site=site,
        name="Pump Controller",
        device_type="plc",
        protocol="modbus_tcp",
        status="online",
    )
    timestamp = timezone.now().replace(microsecond=0)
    TelemetryData.objects.create(device=device, timestamp=timestamp, key="run_status", value_bool=True)

    request = RequestFactory().get(f"/a/{team.slug}/telemetry/api/history/{device.id}/?key=run_status&hours=24")
    request.user = user
    request.team = team
    response = device_telemetry_history_api(request, team.slug, device.id)
    data = json.loads(response.content)

    assert response.status_code == 200
    assert data["values"] == [True]


@pytest.mark.django_db
@pytest.mark.parametrize("hours", ["invalid", "-6", "0"])
def test_telemetry_history_api_handles_invalid_time_windows(hours):
    from apps.telemetry.views import device_telemetry_history_api

    user = CustomUser.objects.create_user(
        username=f"history_window_{hours}",
        email=f"history-window-{hours}@example.com",
        password="password123",
    )
    team = Team.objects.create(name=f"History Window {hours}", slug=f"history-window-{hours}")
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="History Window Site")
    device = Device.objects.create(team=team, site=site, name="Window Meter", protocol="modbus_tcp")
    request = RequestFactory().get(f"/a/{team.slug}/telemetry/api/history/{device.id}/?hours={hours}")
    request.user = user
    request.team = team

    response = device_telemetry_history_api(request, team.slug, device.id)

    assert response.status_code == 200


@pytest.mark.django_db
def test_device_detail_renders_launch_ready_live_telemetry_controls():
    user = CustomUser.objects.create_user(
        username="device_live_controls_user",
        email="device-live-controls@example.com",
        password="password123",
    )
    team = Team.objects.create(name="Device Live Controls", slug="device-live-controls")
    Membership.objects.create(user=user, team=team, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="Live Controls Site")
    device = Device.objects.create(
        team=team,
        site=site,
        name="Live Controls Meter",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="online",
    )
    client = Client()
    client.force_login(user)

    response = client.get(reverse("web_team:devices:device_detail", args=[team.slug, device.id]))

    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="telemetry-history-window"' in content
    assert 'id="telemetry-pause-button"' in content
    assert 'id="telemetry-refresh-button"' in content
    assert 'id="telemetry-sample-filter"' in content
    assert 'id="telemetry-samples-export"' in content
    assert "depends on plan and device capability" in content
    assert "/ws/device/${deviceId}/" in content
    assert "NovenaTelemetryDiagnostics" in content
    assert "cdn.jsdelivr.net/npm/chart.js" not in content


@pytest.mark.django_db
def test_telemetry_csv_export_uses_site_local_timestamp():
    from apps.telemetry.views import export_telemetry_csv
    from apps.utils.timezones import format_site_datetime

    user = CustomUser.objects.create_user(
        username="csv_tz_user",
        email="csv-tz@example.com",
        password="password123",
    )
    team = Team.objects.create(name="CSV TZ Team", slug="csv-tz-team")
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="Tokyo Site", timezone="Asia/Tokyo")
    device = Device.objects.create(
        team=team,
        site=site,
        name="Tokyo Meter",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="online",
    )
    timestamp = timezone.now().replace(microsecond=0)
    TelemetryData.objects.create(
        device=device,
        timestamp=timestamp,
        key="temperature",
        value_numeric=28.0,
    )

    request = RequestFactory().get(f"/a/{team.slug}/telemetry/api/export/{device.id}/?days=7")
    request.user = user
    request.team = team
    response = export_telemetry_csv(request, team.slug, device.id)

    assert response.status_code == 200
    assert format_site_datetime(timestamp, site, "%Y-%m-%d %H:%M:%S %Z") in response.content.decode()


@pytest.mark.django_db
def test_same_utc_telemetry_displays_different_site_local_times():
    from apps.telemetry.views import device_telemetry_history_api
    from apps.utils.timezones import format_site_datetime

    user = CustomUser.objects.create_user(
        username="multi_site_tz_user",
        email="multi-site-tz@example.com",
        password="password123",
    )
    team = Team.objects.create(name="Multi Site TZ Team", slug="multi-site-tz-team")
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)
    singapore_site = Site.objects.create(team=team, name="Singapore Site", timezone="Asia/Singapore")
    auckland_site = Site.objects.create(team=team, name="Auckland Site", timezone="Pacific/Auckland")
    instant = timezone.now().replace(microsecond=0)
    devices = [
        Device.objects.create(
            team=team,
            site=singapore_site,
            name="Singapore Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            status="online",
        ),
        Device.objects.create(
            team=team,
            site=auckland_site,
            name="Auckland Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            status="online",
        ),
    ]
    for device in devices:
        TelemetryData.objects.create(device=device, timestamp=instant, key="temperature", value_numeric=28.0)

    local_labels = []
    for device in devices:
        request = RequestFactory().get(f"/a/{team.slug}/telemetry/api/history/{device.id}/?key=temperature&hours=24")
        request.user = user
        request.team = team
        response = device_telemetry_history_api(request, team.slug, device.id)
        local_labels.append(json.loads(response.content)["labels_local"][0])

    assert local_labels == [
        format_site_datetime(instant, singapore_site, "%H:%M:%S"),
        format_site_datetime(instant, auckland_site, "%H:%M:%S"),
    ]


@pytest.mark.django_db
def test_flush_telemetry_matches_device_id_before_name():
    team = Team.objects.create(name="Match Team", slug="match-team")
    site = Site.objects.create(team=team, name="Match Site")
    gateway = Gateway.objects.create(
        team=team,
        site=site,
        name="Match Gateway",
        serial_number="GW-MATCH-001",
        access_token="tok_match_001",
    )
    claim_gateway(gateway)
    device_by_id = Device.objects.create(
        team=team,
        site=site,
        gateway=gateway,
        name="ID Device",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="offline",
    )
    device_by_name = Device.objects.create(
        team=team,
        site=site,
        gateway=gateway,
        name="Name Device",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="offline",
    )
    payload = buffered_gateway_payload(
        gateway,
        {
            "serial_number": gateway.serial_number,
            "device_id": device_by_id.id,
            "device_name": device_by_name.name,
            "values": {"active_power": 111.0},
        },
    )
    raw_payload = json.dumps(payload).encode("utf-8")
    mock_redis = MagicMock()
    mock_pipeline = MagicMock()
    mock_redis.pipeline.return_value.__enter__.return_value = mock_pipeline
    mock_pipeline.execute.return_value = [[raw_payload], 1]

    with patch("redis.Redis.from_url", return_value=mock_redis):
        flush_telemetry_buffer_task()

    assert TelemetryData.objects.filter(device=device_by_id, key="active_power").exists()
    assert not TelemetryData.objects.filter(device=device_by_name, key="active_power").exists()
    device_by_id.refresh_from_db()
    assert device_by_id.status == "online"


@pytest.mark.django_db
def test_flush_telemetry_logs_legacy_first_device_fallback(caplog):
    team = Team.objects.create(name="Fallback Team", slug="fallback-team")
    site = Site.objects.create(team=team, name="Fallback Site")
    gateway = Gateway.objects.create(
        team=team,
        site=site,
        name="Fallback Gateway",
        serial_number="GW-FALLBACK-001",
        access_token="tok_fallback_001",
    )
    claim_gateway(gateway)
    device = Device.objects.create(
        team=team,
        site=site,
        gateway=gateway,
        name="Only Device",
        device_type="power_meter",
        protocol="modbus_tcp",
        status="offline",
    )
    payload = buffered_gateway_payload(
        gateway,
        {
            "serial_number": gateway.serial_number,
            "values": {"active_power": 222.0},
        },
    )
    raw_payload = json.dumps(payload).encode("utf-8")
    mock_redis = MagicMock()
    mock_pipeline = MagicMock()
    mock_redis.pipeline.return_value.__enter__.return_value = mock_pipeline
    mock_pipeline.execute.return_value = [[raw_payload], 1]

    with patch("redis.Redis.from_url", return_value=mock_redis), caplog.at_level("WARNING", logger="novena_hub"):
        flush_telemetry_buffer_task()

    assert TelemetryData.objects.filter(device=device, key="active_power").exists()
    assert "legacy first-device telemetry fallback" in caplog.text
