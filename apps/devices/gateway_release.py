"""Durable, fail-closed Gateway release and credential revocation."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .models import (
    GatewayConfig,
    GatewayConfigOutbox,
    GatewayInventory,
    GatewayReleaseRequest,
    RemoteCommand,
)

logger = logging.getLogger("novena_hub")


def gateway_is_quarantined(gateway) -> bool:
    return gateway.lifecycle_status in {"release_pending", "released"}


def _schedule_release(release_id):
    try:
        from .tasks import dispatch_gateway_release

        dispatch_gateway_release.delay(release_id)
    except Exception as exc:
        logger.warning("Gateway release %s remains queued: %s", release_id, exc)


@transaction.atomic
def request_gateway_release(gateway, *, requested_by=None) -> GatewayReleaseRequest:
    locked = gateway.__class__.objects.select_for_update().get(pk=gateway.pk)
    existing = locked.release_requests.filter(
        status__in=[
            GatewayReleaseRequest.Status.QUEUED,
            GatewayReleaseRequest.Status.REVOKING,
            GatewayReleaseRequest.Status.RETRY,
            GatewayReleaseRequest.Status.NEEDS_ATTENTION,
        ]
    ).first()
    if existing:
        return existing
    if locked.lifecycle_status == "released":
        raise ValueError("This Gateway has already been released.")

    locked.lifecycle_status = "release_pending"
    locked.status = "offline"
    locked.mqtt_connected = False
    locked.save(update_fields=["lifecycle_status", "status", "mqtt_connected"])
    release = GatewayReleaseRequest.objects.create(
        team=locked.team,
        gateway=locked,
        requested_by=requested_by,
    )
    transaction.on_commit(lambda: _schedule_release(release.pk))
    return release


def _retry_release(release_id, exc):
    with transaction.atomic():
        release = GatewayReleaseRequest.objects.select_for_update().get(pk=release_id)
        delay = min(
            int(getattr(settings, "GATEWAY_RELEASE_RETRY_MAX_SECONDS", 300)),
            max(5, 2 ** min(release.attempt_count, 8)),
        )
        release.status = (
            GatewayReleaseRequest.Status.NEEDS_ATTENTION
            if release.attempt_count >= 3
            else GatewayReleaseRequest.Status.RETRY
        )
        release.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        release.lease_expires_at = None
        release.last_error = str(exc)
        release.save()
    logger.warning("Gateway release %s will retry after verified revocation failed: %s", release_id, exc)


def _cancel_pending_remote_commands(gateway):
    from .remote_control import TERMINAL_STATUSES, transition_command

    for command in RemoteCommand.objects.filter(gateway=gateway).exclude(status__in=TERMINAL_STATUSES):
        transition_command(
            command,
            RemoteCommand.Status.CANCELLED,
            "gateway_release_cancelled",
            evidence={"reason": "Gateway release requested"},
            updates={"error_code": "gateway_released", "error_message": "The Gateway was released."},
        )


@transaction.atomic
def _finalize_release(release_id) -> GatewayReleaseRequest:
    release = GatewayReleaseRequest.objects.select_for_update().select_related("gateway").get(pk=release_id)
    gateway = release.gateway.__class__.objects.select_for_update().get(pk=release.gateway_id)
    if release.status == GatewayReleaseRequest.Status.COMPLETED:
        return release

    _cancel_pending_remote_commands(gateway)

    gateway.activations.exclude(status="acknowledged").update(
        status="superseded",
        last_error="Gateway ownership was released.",
    )
    gateway.activations.update(encrypted_mqtt_password="")
    GatewayConfigOutbox.objects.filter(config__gateway=gateway).exclude(
        status=GatewayConfigOutbox.Status.COMPLETED
    ).update(status=GatewayConfigOutbox.Status.DEAD_LETTER, last_error="Gateway ownership was released.")
    GatewayConfig.objects.filter(gateway=gateway).update(
        status="superseded",
        error_code="gateway_released",
        error_message="The Gateway was released before this configuration completed.",
    )

    # Preserve append-only setup and governed-command evidence on the archived
    # ownership row, while removing ordinary operational/customer data.
    gateway.logs.all().delete()
    gateway.rpc_commands.all().delete()
    gateway.config_history.all().delete()
    gateway.devices.all().delete()

    inventory = GatewayInventory.objects.select_for_update().filter(gateway=gateway).first()
    if inventory:
        inventory.status = "released"
        inventory.claimed_by_team = None
        inventory.claimed_at = None
        inventory.gateway = None
        inventory.save(update_fields=["status", "claimed_by_team", "claimed_at", "gateway"])

    gateway.status = "offline"
    gateway.lifecycle_status = "released"
    gateway.mqtt_username = None
    gateway.mqtt_password = ""
    gateway.client_cert_pem = ""
    gateway.client_key_pem = ""
    gateway.access_token = secrets.token_hex(20)
    gateway.mqtt_provisioning_status = "revoked"
    gateway.mqtt_provisioning_error = ""
    gateway.credential_rotation_status = "not_started"
    gateway.discovery_data = {}
    gateway.config = {}
    gateway.connected_devices = []
    gateway.active_connectors = []
    gateway.last_seen = None
    gateway.last_bootstrap_seen_at = None
    gateway.save()

    release.status = GatewayReleaseRequest.Status.COMPLETED
    release.completed_at = timezone.now()
    release.lease_expires_at = None
    release.last_error = ""
    release.save()
    return release


def dispatch_gateway_release(release_id) -> GatewayReleaseRequest | None:
    now = timezone.now()
    lease_seconds = int(getattr(settings, "GATEWAY_RELEASE_LEASE_SECONDS", 60))
    with transaction.atomic():
        release = (
            GatewayReleaseRequest.objects.select_for_update().select_related("gateway").filter(pk=release_id).first()
        )
        if not release or release.status == GatewayReleaseRequest.Status.COMPLETED:
            return release
        claimable = release.status in {
            GatewayReleaseRequest.Status.QUEUED,
            GatewayReleaseRequest.Status.RETRY,
            GatewayReleaseRequest.Status.NEEDS_ATTENTION,
        }
        claimable = claimable or (
            release.status == GatewayReleaseRequest.Status.REVOKING
            and release.lease_expires_at
            and release.lease_expires_at <= now
        )
        if not claimable or release.next_attempt_at > now:
            return release
        release.status = GatewayReleaseRequest.Status.REVOKING
        release.claimed_at = now
        release.lease_expires_at = now + timedelta(seconds=lease_seconds)
        release.attempt_count += 1
        release.save()

    try:
        if getattr(settings, "MQTT_PROVISIONING_REQUIRED", False):
            from .mqtt_provisioning import deprovision_gateway_mqtt

            deprovision_gateway_mqtt(release.gateway)
        elif settings.DEBUG:
            logger.info(
                "Skipped MQTT dynamic-security revocation for %s in local development mode.",
                release.gateway.serial_number,
            )
        else:
            raise RuntimeError("Gateway release requires MQTT credential revocation outside local development.")
    except Exception as exc:
        _retry_release(release_id, exc)
        return GatewayReleaseRequest.objects.get(pk=release_id)
    return _finalize_release(release_id)


def dispatch_due_gateway_releases(limit=100) -> int:
    now = timezone.now()
    ids = list(
        GatewayReleaseRequest.objects.filter(
            status__in=[
                GatewayReleaseRequest.Status.QUEUED,
                GatewayReleaseRequest.Status.RETRY,
                GatewayReleaseRequest.Status.NEEDS_ATTENTION,
                GatewayReleaseRequest.Status.REVOKING,
            ],
            next_attempt_at__lte=now,
        )
        .filter(models.Q(lease_expires_at__isnull=True) | models.Q(lease_expires_at__lte=now))
        .order_by("next_attempt_at")
        .values_list("pk", flat=True)[:limit]
    )
    for release_id in ids:
        dispatch_gateway_release(release_id)
    return len(ids)
