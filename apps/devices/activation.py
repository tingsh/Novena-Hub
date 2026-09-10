import base64
import hashlib
import logging
import sys
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from .models import GatewayActivation, GatewayInventory

logger = logging.getLogger("novena_hub")


def _activation_cipher() -> Fernet:
    key = getattr(settings, "GATEWAY_ACTIVATION_ENCRYPTION_KEY", "")
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)

    if not settings.DEBUG and "test" not in sys.argv:
        raise ImproperlyConfigured("GATEWAY_ACTIVATION_ENCRYPTION_KEY is required outside debug/test.")

    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_activation_secret(secret: str) -> str:
    return _activation_cipher().encrypt(secret.encode()).decode()


def decrypt_activation_secret(encrypted_secret: str) -> str:
    if not encrypted_secret:
        raise ValueError("Activation secret is no longer available.")
    try:
        return _activation_cipher().decrypt(encrypted_secret.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Activation secret cannot be decrypted.") from exc


def activation_ttl() -> timedelta:
    hours = getattr(settings, "GATEWAY_ACTIVATION_TTL_HOURS", 24)
    return timedelta(hours=hours)


def create_gateway_activation(gateway, operational_password: str, *, status="provisioning") -> GatewayActivation:
    latest_generation = gateway.activations.order_by("-generation").values_list("generation", flat=True).first() or 0
    expires_at = timezone.now() + activation_ttl()
    return GatewayActivation.objects.create(
        team=gateway.team,
        gateway=gateway,
        generation=latest_generation + 1,
        status=status,
        expires_at=expires_at,
        encrypted_mqtt_password=encrypt_activation_secret(operational_password),
    )


def _schedule_activation_provision(activation_id):
    try:
        from .tasks import provision_gateway_activation

        provision_gateway_activation.delay(activation_id)
    except Exception as exc:
        logger.warning("Gateway activation %s remains queued: %s", activation_id, exc)


@transaction.atomic
def queue_gateway_activation(gateway, operational_password: str) -> GatewayActivation:
    locked_gateway = gateway.__class__.objects.select_for_update().get(pk=gateway.pk)
    locked_gateway.activations.filter(status__in=GatewayActivation.UNRESOLVED_STATUSES).update(
        status="superseded",
        encrypted_mqtt_password="",
        last_error="A newer activation generation was issued.",
    )
    activation = create_gateway_activation(locked_gateway, operational_password, status="provisioning")
    transaction.on_commit(lambda: _schedule_activation_provision(activation.pk))
    return activation


def provision_gateway_activation(activation_id) -> bool:
    activation = GatewayActivation.objects.select_related("gateway").filter(pk=activation_id).first()
    if not activation or activation.status in {"acknowledged", "expired", "superseded"}:
        return False
    if activation.expires_at <= timezone.now():
        expire_gateway_activation(activation)
        return False
    password = decrypt_activation_secret(activation.encrypted_mqtt_password)
    provisioning_required = getattr(settings, "MQTT_PROVISIONING_REQUIRED", False)
    try:
        from django.contrib.auth.hashers import make_password

        if provisioning_required:
            from .mqtt_provisioning import provision_gateway_mqtt

            provision_gateway_mqtt(activation.gateway, password)
        else:
            logger.info(
                "Skipped MQTT dynamic-security provisioning for %s because MQTT_PROVISIONING_REQUIRED is false.",
                activation.gateway.serial_number,
            )
    except Exception as exc:
        activation.status = "retry"
        activation.attempt_count += 1
        activation.last_attempt_at = timezone.now()
        activation.last_error = str(exc)
        activation.save()
        gateway = activation.gateway
        gateway.mqtt_provisioning_status = "failed"
        gateway.mqtt_provisioning_error = str(exc)
        gateway.save(update_fields=["mqtt_provisioning_status", "mqtt_provisioning_error"])
        return False

    gateway = activation.gateway
    gateway.mqtt_username = gateway.serial_number
    gateway.mqtt_password = make_password(password)
    gateway.mqtt_provisioning_status = "success"
    gateway.mqtt_provisioning_error = ""
    gateway.mqtt_provisioned_at = timezone.now()
    gateway.credential_rotation_status = "pending"
    gateway.save(
        update_fields=[
            "mqtt_username",
            "mqtt_password",
            "mqtt_provisioning_status",
            "mqtt_provisioning_error",
            "mqtt_provisioned_at",
            "credential_rotation_status",
        ]
    )
    activation.status = "pending"
    activation.last_error = ""
    activation.expires_at = timezone.now() + activation_ttl()
    activation.save(update_fields=["status", "last_error", "expires_at", "updated_at"])
    if gateway.status == "online" or gateway.last_bootstrap_seen_at:
        return deliver_gateway_activation(activation)
    return True


def retry_delay_for_attempts(attempt_count: int) -> timedelta:
    seconds = min(300, max(60, 60 * (2 ** max(0, attempt_count - 1))))
    return timedelta(seconds=seconds)


def deliver_gateway_activation(activation: GatewayActivation, *, retry: bool = False) -> bool:
    """Publish activation and persist the transport-level delivery attempt."""
    now = timezone.now()
    activation = GatewayActivation.objects.select_related("gateway", "team").get(pk=activation.pk)

    if activation.status == "acknowledged":
        return False
    if activation.expires_at <= now:
        expire_gateway_activation(activation)
        return False

    password = decrypt_activation_secret(activation.encrypted_mqtt_password)

    try:
        from apps.telemetry.mqtt_publisher import publish_gateway_activation

        publish_gateway_activation(activation, password)
    except Exception as exc:
        activation.attempt_count += 1
        activation.last_attempt_at = now
        activation.status = "failed"
        activation.last_error = str(exc)
        activation.save(update_fields=["attempt_count", "last_attempt_at", "status", "last_error", "updated_at"])
        logger.info("Gateway activation delivery failed for %s: %s", activation.gateway.serial_number, exc)
        return False

    activation.attempt_count += 1
    activation.last_attempt_at = now
    activation.delivered_at = now
    activation.status = "retried" if retry or activation.attempt_count > 1 else "delivered"
    activation.last_error = ""
    activation.save(
        update_fields=["attempt_count", "last_attempt_at", "delivered_at", "status", "last_error", "updated_at"]
    )

    gateway = activation.gateway
    if gateway.lifecycle_status in ("claimed", "bootstrap_seen"):
        gateway.lifecycle_status = "activating"
        gateway.save(update_fields=["lifecycle_status"])
    return True


def latest_retryable_activation(gateway):
    now = timezone.now()
    return (
        gateway.activations.filter(
            status__in=GatewayActivation.UNRESOLVED_STATUSES,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )


def retry_activation_for_gateway(gateway) -> bool:
    activation = latest_retryable_activation(gateway)
    if not activation:
        return False
    if activation.status in {"provisioning", "retry"}:
        return provision_gateway_activation(activation.pk)
    return deliver_gateway_activation(activation, retry=True)


@transaction.atomic
def reissue_activation_for_gateway(gateway, *, force=False) -> GatewayActivation:
    """Return a live activation or create a fresh credential generation."""
    gateway = gateway.__class__.objects.select_for_update().get(pk=gateway.pk)
    inventory = (
        GatewayInventory.objects.select_for_update()
        .filter(
            gateway=gateway,
            status="claimed",
            claimed_by_team=gateway.team,
        )
        .first()
    )
    if not inventory or gateway.lifecycle_status in {"release_pending", "released"}:
        raise ValueError("This Gateway is not in an activatable claimed state.")

    activation = latest_retryable_activation(gateway)
    if activation and not force:
        transaction.on_commit(lambda: _schedule_activation_provision(activation.pk))
        return activation

    cooldown = timedelta(seconds=int(getattr(settings, "GATEWAY_ACTIVATION_REISSUE_COOLDOWN_SECONDS", 300)))
    latest = gateway.activations.order_by("-created_at").first()
    if latest and latest.created_at + cooldown > timezone.now() and not force:
        return latest

    from .services import generate_operational_mqtt_password

    password = generate_operational_mqtt_password()
    activation = queue_gateway_activation(gateway, password)
    return activation


@transaction.atomic
def acknowledge_gateway_activation(gateway, request_id: str, generation, status: str, error: str = ""):
    try:
        generation = int(generation)
    except (TypeError, ValueError):
        return None
    activation = GatewayActivation.objects.select_for_update().filter(gateway=gateway, request_id=request_id).first()
    if not activation or activation.generation != generation:
        return None

    latest_generation = (
        GatewayActivation.objects.filter(gateway=gateway)
        .order_by("-generation")
        .values_list("generation", flat=True)
        .first()
    )
    if activation.generation != latest_generation or activation.status in {"expired", "superseded"}:
        return None

    if activation.status == "acknowledged":
        return activation

    if status == "success":
        activation.status = "acknowledged"
        activation.acknowledged_at = timezone.now()
        activation.encrypted_mqtt_password = ""
        activation.last_error = ""
        activation.save(
            update_fields=["status", "acknowledged_at", "encrypted_mqtt_password", "last_error", "updated_at"]
        )
    else:
        activation.status = "failed"
        activation.last_error = error or f"Gateway reported activation status: {status}"
        activation.save(update_fields=["status", "last_error", "updated_at"])
    return activation


def expire_gateway_activation(activation: GatewayActivation) -> bool:
    activation = GatewayActivation.objects.get(pk=activation.pk)
    if activation.status in ("acknowledged", "expired"):
        return False
    activation.status = "expired"
    activation.encrypted_mqtt_password = ""
    activation.last_error = activation.last_error or "Activation expired before gateway acknowledgement."
    activation.save(update_fields=["status", "encrypted_mqtt_password", "last_error", "updated_at"])
    return True


def expire_and_retry_gateway_activations() -> dict:
    now = timezone.now()
    expired = 0
    retried = 0

    for activation in GatewayActivation.objects.filter(
        status__in=GatewayActivation.UNRESOLVED_STATUSES,
        expires_at__lte=now,
    ):
        if expire_gateway_activation(activation):
            expired += 1

    retryable = GatewayActivation.objects.select_related("gateway", "team").filter(
        status__in=GatewayActivation.UNRESOLVED_STATUSES,
        expires_at__gt=now,
    )
    for activation in retryable:
        if activation.last_attempt_at:
            next_retry_at = activation.last_attempt_at + retry_delay_for_attempts(activation.attempt_count)
            if next_retry_at > now:
                continue
        if activation.status in {"provisioning", "retry"}:
            delivered = provision_gateway_activation(activation.pk)
        else:
            delivered = deliver_gateway_activation(activation, retry=activation.attempt_count > 0)
        if delivered:
            retried += 1

    return {"expired": expired, "retried": retried}
