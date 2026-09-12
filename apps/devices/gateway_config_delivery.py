"""Reliable, signed Cloud-to-Edge configuration delivery."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .models import GatewayConfig, GatewayConfigOutbox
from .remote_control_crypto import payload_checksum, sign_payload

logger = logging.getLogger("novena_hub")

GUIDED_SETUP_CAPABILITY = "guided_setup_v1"
CONFIG_SCHEMA_VERSION = 1
TERMINAL_CONFIG_STATUSES = {"active", "failed", "rolled_back", "superseded"}
SUPPORTED_CONFIG_ACTIONS = {"full_update", "connector_update", "connector_add", "connector_remove"}


class GatewayConfigUnsupported(ValueError):
    pass


def gateway_supports_guided_setup(gateway) -> bool:
    return bool(
        gateway.remote_control_clock_ready
        and GUIDED_SETUP_CAPABILITY in set(gateway.gateway_capabilities or [])
    )


def ensure_gateway_configurable(gateway):
    if gateway.lifecycle_status in {"release_pending", "released"}:
        raise GatewayConfigUnsupported("This Gateway is being released and cannot receive settings.")
    if not gateway_supports_guided_setup(gateway):
        if not gateway.remote_control_clock_ready:
            raise GatewayConfigUnsupported(
                "The Gateway clock is still synchronizing. Wait for it to become ready before sending settings."
            )
        raise GatewayConfigUnsupported(
            "Update this Gateway before sending settings. Its current software does not support secure Guided Setup."
        )


def validate_gateway_config_payload(gateway, action: str, config: dict):
    if action not in SUPPORTED_CONFIG_ACTIONS:
        raise ValueError("Choose a supported settings action.")
    if not isinstance(config, dict) or not config:
        raise ValueError("Settings must be a non-empty object.")

    if action == "full_update":
        gateway_section = config.get("gateway")
        mqtt_section = config.get("mqtt")
        if not isinstance(gateway_section, dict) or gateway_section.get("serial_number") != gateway.serial_number:
            raise ValueError("Full settings must target this Gateway serial number.")
        if not isinstance(mqtt_section, dict) or not mqtt_section.get("host") or mqtt_section.get("port") is None:
            raise ValueError("Full settings must include the MQTT host and port.")
        try:
            int(mqtt_section["port"])
        except (TypeError, ValueError) as exc:
            raise ValueError("The MQTT port must be an integer.") from exc
        if "connectors" not in config:
            raise ValueError("Full settings must include a connector list.")

    if action in {"full_update", "connector_update"} and "connectors" in config:
        connectors = config["connectors"]
        if not isinstance(connectors, list) or any(
            not isinstance(connector, dict) or not connector.get("type") for connector in connectors
        ):
            raise ValueError("Connectors must be a list of objects with a connector type.")
    elif action == "connector_update" and not config.get("name"):
        raise ValueError("Connector updates must include a connector list or connector name.")

    if action == "connector_add" and (not config.get("name") or not config.get("type")):
        raise ValueError("A connector name and type are required.")
    if action == "connector_remove" and not config.get("name"):
        raise ValueError("Choose the connector to remove.")


def build_signed_config_envelope(config_record: GatewayConfig) -> dict:
    body = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "request_id": str(config_record.request_id),
        "idempotency_key": str(config_record.idempotency_key),
        "target": {"gateway_serial": config_record.gateway.serial_number},
        "revision": config_record.revision,
        "checksum": config_record.checksum,
        "issued_at": config_record.created_at.isoformat(),
        "expires_at": config_record.expires_at.isoformat(),
        "action": config_record.action,
        "config": config_record.config_json,
    }
    key_id, signature = sign_payload(body)
    return {**body, "signing_key_id": key_id, "signature": signature}


def _schedule_config_dispatch(outbox_id):
    try:
        from .tasks import dispatch_gateway_config_outbox

        dispatch_gateway_config_outbox.delay(outbox_id)
    except Exception as exc:
        logger.warning("Gateway config outbox %s remains queued: %s", outbox_id, exc)


@transaction.atomic
def queue_gateway_config(gateway, action: str, config: dict, *, setup_run=None) -> GatewayConfig:
    ensure_gateway_configurable(gateway)
    validate_gateway_config_payload(gateway, action, config)
    locked_gateway = gateway.__class__.objects.select_for_update().get(pk=gateway.pk)
    ensure_gateway_configurable(locked_gateway)
    latest_revision = (
        GatewayConfig.objects.filter(gateway=locked_gateway)
        .order_by("-revision")
        .values_list("revision", flat=True)
        .first()
        or 0
    )
    older = GatewayConfig.objects.filter(
        gateway=locked_gateway,
        status__in=["queued", "waiting_for_gateway", "published", "accepted", "timed_out"],
    )
    older_ids = list(older.values_list("pk", flat=True))
    older.update(
        status="superseded",
        error_code="newer_revision",
        error_message="A newer settings revision replaced this one.",
    )
    GatewayConfigOutbox.objects.filter(config_id__in=older_ids).update(status=GatewayConfigOutbox.Status.COMPLETED)

    ttl = int(getattr(settings, "GATEWAY_CONFIG_INTENT_TTL_SECONDS", 86400))
    config_record = GatewayConfig.objects.create(
        team=locked_gateway.team,
        gateway=locked_gateway,
        setup_run=setup_run,
        config_json=config,
        request_id=uuid.uuid4(),
        action=action,
        revision=latest_revision + 1,
        checksum=payload_checksum(config),
        expires_at=timezone.now() + timedelta(seconds=ttl),
        status="queued",
    )
    config_record.envelope_json = build_signed_config_envelope(config_record)
    config_record.save(update_fields=["envelope_json", "updated_at"])
    outbox = GatewayConfigOutbox.objects.create(team=locked_gateway.team, config=config_record)
    transaction.on_commit(lambda: _schedule_config_dispatch(outbox.pk))
    return config_record


def _mark_config_timed_out(outbox, config_record, message):
    now = timezone.now()
    outbox.status = GatewayConfigOutbox.Status.DEAD_LETTER
    outbox.dead_lettered_at = now
    outbox.lease_expires_at = None
    outbox.last_error = message
    config_record.status = "timed_out"
    config_record.error_code = "gateway_ack_timeout"
    config_record.error_message = message
    outbox.save()
    config_record.save()


def dispatch_gateway_config_outbox(outbox_id: int) -> GatewayConfig | None:
    now = timezone.now()
    lease_seconds = int(getattr(settings, "GATEWAY_CONFIG_OUTBOX_LEASE_SECONDS", 60))
    with transaction.atomic():
        outbox = GatewayConfigOutbox.objects.select_for_update().filter(pk=outbox_id).first()
        if not outbox:
            return None
        config_record = GatewayConfig.objects.select_for_update().select_related("gateway").get(pk=outbox.config_id)
        if config_record.status in TERMINAL_CONFIG_STATUSES:
            outbox.status = GatewayConfigOutbox.Status.COMPLETED
            outbox.lease_expires_at = None
            outbox.save(update_fields=["status", "lease_expires_at", "updated_at"])
            return config_record
        if config_record.expires_at and config_record.expires_at <= now:
            _mark_config_timed_out(outbox, config_record, "The Gateway did not apply these settings within 24 hours.")
            return config_record
        if config_record.status == "accepted":
            if config_record.acknowledgement_deadline_at and config_record.acknowledgement_deadline_at <= now:
                _mark_config_timed_out(
                    outbox,
                    config_record,
                    "The Gateway accepted these settings but did not finish applying them.",
                )
            return config_record

        claimable = outbox.status in {
            GatewayConfigOutbox.Status.PENDING,
            GatewayConfigOutbox.Status.RETRY,
            GatewayConfigOutbox.Status.WAITING_GATEWAY,
            GatewayConfigOutbox.Status.AWAITING_ACK,
        }
        claimable = claimable or (
            outbox.status == GatewayConfigOutbox.Status.CLAIMED
            and outbox.lease_expires_at
            and outbox.lease_expires_at <= now
        )
        if not claimable or outbox.next_attempt_at > now:
            return config_record

        if config_record.gateway.lifecycle_status in {"release_pending", "released"}:
            config_record.status = "superseded"
            config_record.error_code = "gateway_released"
            config_record.error_message = "The Gateway is being released."
            outbox.status = GatewayConfigOutbox.Status.COMPLETED
            config_record.save()
            outbox.save()
            return config_record

        if config_record.gateway.freshness.status != "live":
            outbox.status = GatewayConfigOutbox.Status.WAITING_GATEWAY
            outbox.next_attempt_at = now + timedelta(seconds=30)
            outbox.lease_expires_at = None
            config_record.status = "waiting_for_gateway"
            config_record.error_code = "gateway_offline"
            config_record.error_message = "Waiting for the Gateway to come online."
            outbox.save()
            config_record.save()
            return config_record

        outbox.status = GatewayConfigOutbox.Status.CLAIMED
        outbox.claimed_at = now
        outbox.lease_expires_at = now + timedelta(seconds=lease_seconds)
        outbox.attempt_count += 1
        outbox.save()

    try:
        from apps.telemetry.mqtt_publisher import publish_config_envelope

        publish_config_envelope(config_record.gateway, config_record.envelope_json)
    except Exception as exc:
        with transaction.atomic():
            outbox = GatewayConfigOutbox.objects.select_for_update().get(pk=outbox_id)
            config_record = GatewayConfig.objects.select_for_update().get(pk=outbox.config_id)
            backoff = min(300, max(5, 2 ** min(outbox.attempt_count, 8)))
            outbox.status = GatewayConfigOutbox.Status.RETRY
            outbox.next_attempt_at = timezone.now() + timedelta(seconds=backoff)
            outbox.lease_expires_at = None
            outbox.last_error = str(exc)
            outbox.save()
            config_record.error_code = "broker_publish_failed"
            config_record.error_message = "Novena could not send the settings yet. It will retry automatically."
            config_record.save()
            return config_record

    with transaction.atomic():
        outbox = GatewayConfigOutbox.objects.select_for_update().get(pk=outbox_id)
        config_record = GatewayConfig.objects.select_for_update().get(pk=outbox.config_id)
        published_at = timezone.now()
        backoff = min(300, max(15, 2 ** min(outbox.attempt_count, 8)))
        outbox.status = GatewayConfigOutbox.Status.AWAITING_ACK
        outbox.delivered_at = published_at
        outbox.next_attempt_at = published_at + timedelta(seconds=backoff)
        outbox.lease_expires_at = None
        outbox.last_error = ""
        outbox.save()
        config_record.status = "published"
        config_record.published_at = config_record.published_at or published_at
        config_record.delivered_at = config_record.delivered_at or published_at
        config_record.error_code = ""
        config_record.error_message = ""
        config_record.save()
        return config_record


@transaction.atomic
def acknowledge_gateway_config(gateway, attrs: dict) -> GatewayConfig:
    request_id = attrs.get("config_update_request_id")
    revision = attrs.get("config_revision")
    checksum = attrs.get("config_checksum")
    idempotency = attrs.get("config_idempotency_key")
    if not all([request_id, revision is not None, checksum, idempotency]):
        raise ValueError("Signed configuration acknowledgement is missing identity fields.")
    config_record = GatewayConfig.objects.select_for_update().get(request_id=request_id, gateway=gateway)
    if (
        int(revision) != config_record.revision
        or checksum != config_record.checksum
        or str(idempotency) != str(config_record.idempotency_key)
    ):
        raise ValueError("Signed configuration acknowledgement identity does not match.")

    now = timezone.now()
    incoming = attrs.get("config_update_status", "unknown")
    incoming = "active" if incoming == "success" else incoming
    if incoming not in {"accepted", "active", "failed", "rolled_back"}:
        raise ValueError("Signed configuration acknowledgement has an invalid status.")

    latest_revision = (
        GatewayConfig.objects.filter(gateway=gateway).order_by("-revision").values_list("revision", flat=True).first()
    )
    config_record.last_ack_at = now
    if config_record.revision != latest_revision:
        config_record.status = "superseded"
        config_record.save(update_fields=["status", "last_ack_at", "updated_at"])
        return config_record

    if config_record.status in {"active", "failed", "rolled_back"} and incoming == "accepted":
        config_record.save(update_fields=["last_ack_at", "updated_at"])
        return config_record
    if config_record.status == "timed_out" and incoming == "accepted":
        config_record.save(update_fields=["last_ack_at", "updated_at"])
        return config_record

    config_record.status = incoming
    if incoming == "accepted":
        config_record.accepted_at = now
        config_record.acknowledgement_deadline_at = now + timedelta(
            seconds=int(getattr(settings, "GATEWAY_CONFIG_APPLY_TIMEOUT_SECONDS", 300))
        )
        outbox = getattr(config_record, "outbox", None)
        if outbox:
            outbox.status = GatewayConfigOutbox.Status.AWAITING_ACK
            outbox.next_attempt_at = config_record.acknowledgement_deadline_at
            outbox.save()
    else:
        config_record.acknowledged_at = now
        config_record.technical_error = attrs.get("config_update_error", "") or ""
        config_record.error_code = attrs.get("config_update_error_code", "") or ""
        config_record.rollback_performed = bool(attrs.get("rollback_performed", False))
        config_record.connector_results = attrs.get("connector_results", []) or []
        config_record.rollback_connector_results = attrs.get("rollback_connector_results", []) or []
        outbox = getattr(config_record, "outbox", None)
        if outbox:
            outbox.status = GatewayConfigOutbox.Status.COMPLETED
            outbox.lease_expires_at = None
            outbox.save()
    config_record.save()
    return config_record


def retry_gateway_config(config_record: GatewayConfig) -> GatewayConfig:
    if config_record.status not in {"failed", "rolled_back", "timed_out"}:
        raise ValueError("Only settings that need attention can be retried.")
    return queue_gateway_config(
        config_record.gateway,
        config_record.action,
        config_record.config_json,
        setup_run=config_record.setup_run,
    )


def dispatch_due_gateway_config_outboxes(limit=100) -> int:
    now = timezone.now()
    ids = list(
        GatewayConfigOutbox.objects.filter(
            status__in=[
                GatewayConfigOutbox.Status.PENDING,
                GatewayConfigOutbox.Status.RETRY,
                GatewayConfigOutbox.Status.WAITING_GATEWAY,
                GatewayConfigOutbox.Status.AWAITING_ACK,
                GatewayConfigOutbox.Status.CLAIMED,
            ],
            next_attempt_at__lte=now,
        )
        .filter(models.Q(lease_expires_at__isnull=True) | models.Q(lease_expires_at__lte=now))
        .order_by("next_attempt_at")
        .values_list("pk", flat=True)[:limit]
    )
    for outbox_id in ids:
        dispatch_gateway_config_outbox(outbox_id)
    return len(ids)
