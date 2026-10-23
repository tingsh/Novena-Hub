import json
import os
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import redis
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

Status = Literal["OK", "WARN", "FAIL"]

INSECURE_SECRET_KEY = "django-insecure-yGgc2F6ItlonpCKbwDYLENz2r1DAIAWzZ343C3tA"
DEFAULT_GATEWAY_CLAIM_SECRET = "change-me-in-production"
DEFAULT_DYNSEC_USER = "dynsec-admin"
DEFAULT_DYNSEC_PASS = "dynsec-password"
REQUIRED_HOSTS = {"novenaplatform.com", "app.novenaplatform.com"}
REQUIRED_CELERY_TASKS = {
    "apps.devices.tasks.dispatch_due_gateway_config_outboxes",
    "apps.devices.tasks.dispatch_due_gateway_releases",
    "apps.devices.tasks.dispatch_due_plan_reconciliations",
    "apps.devices.tasks.dispatch_due_remote_command_outboxes",
    "apps.maintenance.tasks.generate_preventive_tickets",
    "apps.telemetry.tasks.flush_telemetry_buffer_task",
    "apps.telemetry.tasks.flush_logs_buffer_task",
    "apps.devices.tasks.check_device_heartbeats",
    "apps.devices.tasks.check_gateway_heartbeats",
    "apps.devices.tasks.expire_and_retry_gateway_activations",
    "apps.devices.tasks.expire_control_activations",
    "apps.impact.tasks.dispatch_impact_refreshes",
    "apps.impact.tasks.dispatch_daily_impact_finalization",
    "apps.impact.tasks.dispatch_monthly_impact_reports",
    "apps.impact.tasks.cleanup_impact_history",
}
SHARED_GATEWAY_INBOUND_TOPICS = {
    "v1/gateway/telemetry",
    "v1/gateway/logs",
    "v1/gateway/attributes",
    "v1/gateway/rpc/response",
}


@dataclass
class CheckResult:
    group: str
    name: str
    status: Status
    detail: str


def _has_value(value) -> bool:
    return value is not None and str(value).strip() != ""


def _check_database() -> CheckResult:
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            cursor.fetchone()
    except Exception as exc:
        return CheckResult("Data", "Database connection", "FAIL", str(exc))
    return CheckResult("Data", "Database connection", "OK", "Database accepted a simple query.")


def _check_timescale() -> CheckResult:
    if connection.vendor != "postgresql":
        return CheckResult("Data", "TimescaleDB hypertable", "FAIL", "Default database is not PostgreSQL.")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'timescaledb';")
            extension = cursor.fetchone()
            cursor.execute(
                "SELECT hypertable_name FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'telemetry_telemetrydata';"
            )
            hypertable = cursor.fetchone()
    except Exception as exc:
        return CheckResult("Data", "TimescaleDB hypertable", "FAIL", str(exc))
    if not extension:
        return CheckResult("Data", "TimescaleDB hypertable", "FAIL", "timescaledb extension is missing.")
    if not hypertable:
        return CheckResult("Data", "TimescaleDB hypertable", "FAIL", "telemetry_telemetrydata is not a hypertable.")
    return CheckResult(
        "Data",
        "TimescaleDB hypertable",
        "OK",
        "TimescaleDB extension and telemetry hypertable verified.",
    )


def _check_redis() -> CheckResult:
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
    except Exception as exc:
        return CheckResult("Services", "Redis", "FAIL", str(exc))
    return CheckResult("Services", "Redis", "OK", f"Redis reachable at {settings.REDIS_URL}.")


def _check_mqtt() -> CheckResult:
    host = settings.MQTT_BROKER_HOST
    port = int(settings.MQTT_BROKER_PORT)
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except Exception as exc:
        return CheckResult("Services", "MQTT broker", "FAIL", f"{host}:{port} is not reachable: {exc}")
    return CheckResult("Services", "MQTT broker", "OK", f"{host}:{port} accepted a TCP connection.")


def _check_legacy_shared_inbound_disabled() -> CheckResult:
    enabled = getattr(settings, "MQTT_ACCEPT_LEGACY_SHARED_INBOUND", False)
    return CheckResult(
        "Services",
        "Legacy shared MQTT inbound bridge",
        "FAIL" if enabled else "OK",
        "MQTT_ACCEPT_LEGACY_SHARED_INBOUND must be False for production customer traffic."
        if enabled
        else "Legacy shared inbound bridge is disabled.",
    )


def _candidate_dynsec_files() -> list[Path]:
    base_dir = Path(settings.BASE_DIR)
    return [
        base_dir / "deploy" / "mosquitto" / "data" / "dynamic-security.json",
        base_dir / "deploy" / "mosquitto" / "dynamic-security.example.json",
        base_dir / "mosquitto" / "dynamic-security.json",
    ]


def _check_gateway_shared_inbound_acls() -> CheckResult:
    base_dir = Path(settings.BASE_DIR)
    checked = []
    unsafe = []
    for path in _candidate_dynsec_files():
        if not path.exists():
            continue
        checked.append(str(path.relative_to(base_dir)) if path.is_relative_to(base_dir) else str(path))
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            return CheckResult("Services", "Gateway MQTT inbound ACLs", "FAIL", f"{path}: {exc}")
        for role in data.get("roles", []):
            role_name = role.get("roleName", "")
            if role_name in {"admin", "cloud-internal"}:
                continue
            for acl in role.get("acls", []):
                if acl.get("acltype") == "publishClientSend" and acl.get("topic") in SHARED_GATEWAY_INBOUND_TOPICS:
                    unsafe.append(f"{role_name}:{acl.get('topic')}")
    if unsafe:
        return CheckResult(
            "Services",
            "Gateway MQTT inbound ACLs",
            "FAIL",
            f"Shared inbound publish ACLs remain: {', '.join(sorted(unsafe))}",
        )
    return CheckResult(
        "Services",
        "Gateway MQTT inbound ACLs",
        "OK",
        f"No shared Gateway inbound publish ACLs found in {checked or 'available dynsec files'}.",
    )


def _check_remote_control_configuration() -> list[CheckResult]:
    key_id = getattr(settings, "REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID", "")
    key_map = getattr(settings, "REMOTE_CONTROL_SIGNING_KEYS", {})
    legacy_key = getattr(settings, "REMOTE_CONTROL_SIGNING_PRIVATE_KEY", "")
    signing_ready = (
        _has_value(key_id) and key_id != "unconfigured" and (_has_value(key_map.get(key_id)) or _has_value(legacy_key))
    )
    retention = int(getattr(settings, "REMOTE_CONTROL_AUDIT_RETENTION_DAYS", 0))
    config_ttl = int(getattr(settings, "GATEWAY_CONFIG_ENVELOPE_TTL_SECONDS", 0))
    config_attempts = int(getattr(settings, "GATEWAY_CONFIG_OUTBOX_MAX_ATTEMPTS", 0))
    telemetry_timeout = int(getattr(settings, "GUIDED_SETUP_FIRST_TELEMETRY_TIMEOUT_SECONDS", 0))
    from apps.devices.models import GatewayControlPolicyBundle
    from apps.teams.models import Team

    unsafe_teams = []
    for team in Team.objects.filter(remote_control_mode=Team.RemoteControlMode.CONTROLLED):
        if not GatewayControlPolicyBundle.objects.filter(
            team=team,
            is_active=True,
            acknowledged_at__isnull=False,
            control_epoch=team.remote_control_epoch,
        ).exists():
            unsafe_teams.append(team.slug)
    return [
        CheckResult(
            "Remote control",
            "Managed command signing key",
            "OK" if signing_ready else "FAIL",
            f"active_key_id={key_id}; key material is {'configured' if signing_ready else 'missing/placeholder'}",
        ),
        CheckResult(
            "Remote control",
            "Audit retention",
            "OK" if retention >= 2555 else "FAIL",
            f"REMOTE_CONTROL_AUDIT_RETENTION_DAYS={retention}; minimum is 2555 (seven years).",
        ),
        CheckResult(
            "Remote control",
            "Controlled-team policy acknowledgement",
            "OK" if not unsafe_teams else "FAIL",
            f"unsafe teams={unsafe_teams or 'none'}",
        ),
        CheckResult(
            "Guided Setup",
            "Configuration envelope lifetime",
            "OK" if 60 <= config_ttl <= 900 else "FAIL",
            f"GATEWAY_CONFIG_ENVELOPE_TTL_SECONDS={config_ttl}; expected 60-900 seconds.",
        ),
        CheckResult(
            "Guided Setup",
            "Configuration delivery retries",
            "OK" if 1 <= config_attempts <= 10 else "FAIL",
            f"GATEWAY_CONFIG_OUTBOX_MAX_ATTEMPTS={config_attempts}; expected 1-10.",
        ),
        CheckResult(
            "Guided Setup",
            "First telemetry timeout",
            "OK" if 30 <= telemetry_timeout <= 3600 else "FAIL",
            f"GUIDED_SETUP_FIRST_TELEMETRY_TIMEOUT_SECONDS={telemetry_timeout}; expected 30-3600 seconds.",
        ),
    ]


def _check_business_impact_reporting() -> list[CheckResult]:
    storage_config = settings.STORAGES.get("impact_reports", {})
    backend = storage_config.get("BACKEND", "")
    private_root = Path(getattr(settings, "PRIVATE_MEDIA_ROOT", settings.BASE_DIR / "private_media"))
    media_root = Path(settings.MEDIA_ROOT)
    storage_private = bool(backend) and backend != "apps.web.storage_backends.PublicMediaStorage"
    filesystem_separate = private_root.resolve() != media_root.resolve()
    try:
        import weasyprint  # noqa: F401

        pdf_ready = True
        pdf_detail = "WeasyPrint imported successfully."
    except Exception as exc:
        pdf_ready = False
        pdf_detail = str(exc)
    return [
        CheckResult(
            "Business impact",
            "Private report storage",
            "OK" if storage_private and filesystem_separate else "FAIL",
            f"backend={backend or 'missing'}; private_root={private_root}",
        ),
        CheckResult(
            "Business impact",
            "PDF rendering",
            "OK" if pdf_ready else "FAIL",
            pdf_detail,
        ),
    ]


def _check_template_help_storage() -> list[CheckResult]:
    config = settings.STORAGES.get("template_request_documents", {})
    backend = config.get("BACKEND", "")
    location = config.get("OPTIONS", {}).get("location", "")
    if backend == "django.core.files.storage.FileSystemStorage":
        private = bool(location) and Path(location).resolve().is_relative_to(
            Path(settings.PRIVATE_MEDIA_ROOT).resolve()
        ) and not Path(location).resolve().is_relative_to(Path(settings.MEDIA_ROOT).resolve())
    else:
        private = backend == "apps.web.storage_backends.PrivateMediaStorage" and str(location).startswith("private/")
    return [CheckResult(
        "Template help", "Private request documents", "OK" if private else "FAIL",
        f"backend={backend or 'missing'}; location={location or 'missing'}",
    )]


def build_checks() -> list[CheckResult]:
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    hosts = set(settings.ALLOWED_HOSTS)
    csrf_origins = set(getattr(settings, "CSRF_TRUSTED_ORIGINS", []))
    celery_tasks = {entry.get("task") for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    app_base_url = getattr(settings, "PROJECT_METADATA", {}).get("URL", "")
    missing_celery_tasks = sorted(REQUIRED_CELERY_TASKS - celery_tasks)
    missing_hosts = sorted(REQUIRED_HOSTS - hosts)
    missing_csrf = sorted(f"https://{host}" for host in REQUIRED_HOSTS if f"https://{host}" not in csrf_origins)
    whatsapp_provider = getattr(settings, "WHATSAPP_PROVIDER", "mock")
    whatsapp_meta_configured = all(
        [
            _has_value(settings.WHATSAPP_PHONE_NUMBER_ID),
            _has_value(settings.WHATSAPP_ACCESS_TOKEN),
            _has_value(settings.WHATSAPP_VERIFY_TOKEN),
            _has_value(settings.WHATSAPP_ALERT_TEMPLATE_NAME),
        ]
    )
    if whatsapp_provider != "meta":
        whatsapp_detail = "WhatsApp is in mock mode."
    elif whatsapp_meta_configured:
        whatsapp_detail = "Meta WhatsApp settings are configured."
    else:
        whatsapp_detail = "Meta WhatsApp settings are required when WHATSAPP_PROVIDER=meta."
    checks: list[CheckResult] = [
        CheckResult(
            "Django",
            "Production settings module",
            "OK" if "production" in settings_module else "FAIL",
            settings_module or "DJANGO_SETTINGS_MODULE is not set.",
        ),
        CheckResult(
            "Django",
            "DEBUG disabled",
            "OK" if not settings.DEBUG else "FAIL",
            f"DEBUG={settings.DEBUG}",
        ),
        CheckResult(
            "Django",
            "SECRET_KEY",
            "OK" if settings.SECRET_KEY != INSECURE_SECRET_KEY else "FAIL",
            "SECRET_KEY is custom."
            if settings.SECRET_KEY != INSECURE_SECRET_KEY
            else "Default insecure SECRET_KEY is active.",
        ),
        CheckResult(
            "Django",
            "Gateway claim secret",
            "OK" if settings.GATEWAY_CLAIM_SECRET != DEFAULT_GATEWAY_CLAIM_SECRET else "FAIL",
            "GATEWAY_CLAIM_SECRET is custom."
            if settings.GATEWAY_CLAIM_SECRET != DEFAULT_GATEWAY_CLAIM_SECRET
            else "Default gateway claim secret is active.",
        ),
        CheckResult(
            "Django",
            "Gateway activation encryption key",
            "OK" if _has_value(getattr(settings, "GATEWAY_ACTIVATION_ENCRYPTION_KEY", "")) else "FAIL",
            "GATEWAY_ACTIVATION_ENCRYPTION_KEY is configured."
            if _has_value(getattr(settings, "GATEWAY_ACTIVATION_ENCRYPTION_KEY", ""))
            else "GATEWAY_ACTIVATION_ENCRYPTION_KEY is required for activation escrow.",
        ),
        CheckResult(
            "Django",
            "Allowed hosts",
            "OK" if "*" not in hosts and not missing_hosts else "FAIL",
            f"ALLOWED_HOSTS={sorted(hosts)}; missing={missing_hosts or 'none'}",
        ),
        CheckResult(
            "Django",
            "APP_BASE_URL HTTPS",
            "OK" if str(app_base_url).startswith("https://") else "FAIL",
            str(app_base_url),
        ),
        CheckResult(
            "Django",
            "Absolute HTTPS URLs",
            "OK" if settings.USE_HTTPS_IN_ABSOLUTE_URLS else "FAIL",
            f"USE_HTTPS_IN_ABSOLUTE_URLS={settings.USE_HTTPS_IN_ABSOLUTE_URLS}",
        ),
        CheckResult(
            "Django",
            "CSRF trusted origins",
            "OK" if not missing_csrf else "FAIL",
            f"missing={missing_csrf or 'none'}",
        ),
        CheckResult(
            "Services",
            "Celery Beat schedule",
            "OK" if not missing_celery_tasks else "FAIL",
            f"missing={missing_celery_tasks or 'none'}",
        ),
        CheckResult(
            "Services",
            "MQTT Dynamic Security credentials",
            "OK"
            if settings.MQTT_DYNSEC_ADMIN_USER != DEFAULT_DYNSEC_USER
            and settings.MQTT_DYNSEC_ADMIN_PASS != DEFAULT_DYNSEC_PASS
            else "FAIL",
            "Dynamic Security admin credentials are customized."
            if settings.MQTT_DYNSEC_ADMIN_USER != DEFAULT_DYNSEC_USER
            and settings.MQTT_DYNSEC_ADMIN_PASS != DEFAULT_DYNSEC_PASS
            else "Default Dynamic Security admin credentials are active.",
        ),
        CheckResult(
            "Services",
            "Required Gateway MQTT provisioning",
            "OK" if getattr(settings, "MQTT_PROVISIONING_REQUIRED", False) else "FAIL",
            "MQTT_PROVISIONING_REQUIRED=True"
            if getattr(settings, "MQTT_PROVISIONING_REQUIRED", False)
            else "MQTT_PROVISIONING_REQUIRED must be True for managed Gateways.",
        ),
        CheckResult(
            "Notifications",
            "Amazon SES",
            "OK"
            if all(
                [
                    _has_value(settings.AWS_SES_REGION_NAME),
                    _has_value(settings.AWS_SES_ACCESS_KEY_ID),
                    _has_value(settings.AWS_SES_SECRET_ACCESS_KEY),
                    _has_value(settings.DEFAULT_FROM_EMAIL),
                ]
            )
            else "FAIL",
            "SES region, credentials, and sender are configured.",
        ),
        CheckResult(
            "Notifications",
            "WhatsApp",
            "OK" if whatsapp_provider != "meta" or whatsapp_meta_configured else "FAIL",
            whatsapp_detail,
        ),
        CheckResult(
            "Operations",
            "Health check token",
            "OK" if settings.HEALTH_CHECK_TOKENS else "FAIL",
            "HEALTH_CHECK_TOKENS configured." if settings.HEALTH_CHECK_TOKENS else "HEALTH_CHECK_TOKENS is empty.",
        ),
        CheckResult(
            "Billing",
            "Stripe launch mode",
            "WARN" if not settings.STRIPE_LIVE_MODE else "OK",
            "Stripe is in test mode; fine for unpaid pilots, not for paid launch."
            if not settings.STRIPE_LIVE_MODE
            else "Stripe live mode enabled.",
        ),
        CheckResult(
            "Billing",
            "Stripe webhook secret",
            "WARN" if not _has_value(settings.DJSTRIPE_WEBHOOK_SECRET) else "OK",
            "DJSTRIPE_WEBHOOK_SECRET is empty."
            if not _has_value(settings.DJSTRIPE_WEBHOOK_SECRET)
            else "Webhook secret configured.",
        ),
        CheckResult(
            "Operations",
            "Sentry",
            "WARN" if not _has_value(settings.SENTRY_DSN) else "OK",
            "SENTRY_DSN is empty." if not _has_value(settings.SENTRY_DSN) else "Sentry configured.",
        ),
    ]
    checks.extend([_check_database(), _check_timescale(), _check_redis(), _check_mqtt()])
    checks.extend([_check_legacy_shared_inbound_disabled(), _check_gateway_shared_inbound_acls()])
    checks.extend(_check_remote_control_configuration())
    checks.extend(_check_business_impact_reporting())
    checks.extend(_check_template_help_storage())
    return checks


class Command(BaseCommand):
    help = "Checks whether Novena Hub is ready for the lean production VPS deployment."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")

    def handle(self, *args, **options):
        checks = build_checks()
        has_failures = any(check.status == "FAIL" for check in checks)

        if options["json"]:
            self.stdout.write(json.dumps([asdict(check) for check in checks], indent=2))
        else:
            current_group = None
            for check in checks:
                if check.group != current_group:
                    current_group = check.group
                    self.stdout.write(f"\n{current_group}")
                style = {
                    "OK": self.style.SUCCESS,
                    "WARN": self.style.WARNING,
                    "FAIL": self.style.ERROR,
                }[check.status]
                self.stdout.write(style(f"  {check.status:<4} {check.name}: {check.detail}"))

        if has_failures and options["json"]:
            sys.exit(1)
        if has_failures:
            raise CommandError("Production readiness check failed.")
