import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.web.management.commands import production_readiness_check as readiness

OK_EXTERNAL_CHECKS = [
    readiness.CheckResult("Data", "Database connection", "OK", "patched"),
    readiness.CheckResult("Data", "TimescaleDB hypertable", "OK", "patched"),
    readiness.CheckResult("Services", "Redis", "OK", "patched"),
    readiness.CheckResult("Services", "MQTT broker", "OK", "patched"),
    [
        readiness.CheckResult("Business impact", "Private report storage", "OK", "patched"),
        readiness.CheckResult("Business impact", "PDF rendering", "OK", "patched"),
    ],
]


PRODUCTION_SETTINGS = {
    "DEBUG": False,
    "SECRET_KEY": "prod-secret",
    "GATEWAY_CLAIM_SECRET": "gateway-secret",
    "GATEWAY_ACTIVATION_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    "ALLOWED_HOSTS": ["novenaplatform.com", "app.novenaplatform.com"],
    "PROJECT_METADATA": {"URL": "https://app.novenaplatform.com"},
    "USE_HTTPS_IN_ABSOLUTE_URLS": True,
    "CSRF_TRUSTED_ORIGINS": ["https://novenaplatform.com", "https://app.novenaplatform.com"],
    "MQTT_DYNSEC_ADMIN_USER": "novena-dynsec-admin",
    "MQTT_DYNSEC_ADMIN_PASS": "novena-dynsec-password",
    "MQTT_PROVISIONING_REQUIRED": True,
    "MQTT_ACCEPT_LEGACY_SHARED_INBOUND": False,
    "AWS_SES_REGION_NAME": "ap-southeast-1",
    "AWS_SES_ACCESS_KEY_ID": "ses-key",
    "AWS_SES_SECRET_ACCESS_KEY": "ses-secret",
    "EMAIL_BACKEND": "anymail.backends.amazon_ses.EmailBackend",
    "DEFAULT_FROM_EMAIL": "Novena <no-reply@alerts.novenaplatform.com>",
    "ACCOUNT_EMAIL_VERIFICATION": "mandatory",
    "ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED": True,
    "WHATSAPP_PROVIDER": "meta",
    "WHATSAPP_PHONE_NUMBER_ID": "phone-id",
    "WHATSAPP_ACCESS_TOKEN": "token",
    "WHATSAPP_VERIFY_TOKEN": "verify",
    "WHATSAPP_ALERT_TEMPLATE_NAME": "novena_alert_notification",
    "HEALTH_CHECK_TOKENS": ["health-token"],
    "STRIPE_LIVE_MODE": False,
    "DJSTRIPE_WEBHOOK_SECRET": "",
    "SENTRY_DSN": "",
    "REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID": "production-key",
    "REMOTE_CONTROL_SIGNING_KEYS": {"production-key": "base64-seed"},
    "REMOTE_CONTROL_SIGNING_PRIVATE_KEY": "",
    "REMOTE_CONTROL_AUDIT_RETENTION_DAYS": 2555,
}


def _patch_external_checks():
    return patch.multiple(
        readiness,
        _check_database=lambda: OK_EXTERNAL_CHECKS[0],
        _check_timescale=lambda: OK_EXTERNAL_CHECKS[1],
        _check_redis=lambda: OK_EXTERNAL_CHECKS[2],
        _check_mqtt=lambda: OK_EXTERNAL_CHECKS[3],
        _check_business_impact_reporting=lambda: OK_EXTERNAL_CHECKS[4],
    )


class ProductionReadinessCommandTest(TestCase):
    @override_settings(**PRODUCTION_SETTINGS)
    @patch.dict("os.environ", {"DJANGO_SETTINGS_MODULE": "novena_hub.settings_production"})
    def test_command_passes_with_production_safe_settings_and_warnings(self):
        output = io.StringIO()

        with _patch_external_checks():
            call_command("production_readiness_check", stdout=output)

        rendered = output.getvalue()
        self.assertIn("OK   DEBUG disabled", rendered)
        self.assertIn("WARN Stripe launch mode", rendered)

    @override_settings(**PRODUCTION_SETTINGS)
    @patch.dict("os.environ", {"DJANGO_SETTINGS_MODULE": "novena_hub.settings_production"})
    def test_json_output_is_machine_readable(self):
        output = io.StringIO()

        with _patch_external_checks():
            call_command("production_readiness_check", "--json", stdout=output)

        payload = json.loads(output.getvalue())
        self.assertTrue(any(check["name"] == "TimescaleDB hypertable" for check in payload))
        self.assertTrue(all(check["status"] != "FAIL" for check in payload))

    @override_settings(
        **{
            **PRODUCTION_SETTINGS,
            "DEBUG": True,
            "SECRET_KEY": readiness.INSECURE_SECRET_KEY,
            "GATEWAY_CLAIM_SECRET": readiness.DEFAULT_GATEWAY_CLAIM_SECRET,
            "GATEWAY_ACTIVATION_ENCRYPTION_KEY": "",
            "ALLOWED_HOSTS": ["*"],
            "HEALTH_CHECK_TOKENS": [],
            "AWS_SES_ACCESS_KEY_ID": "",
            "AWS_SES_SECRET_ACCESS_KEY": "",
            "EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend",
            "ACCOUNT_EMAIL_VERIFICATION": "optional",
            "ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED": False,
            "WHATSAPP_PHONE_NUMBER_ID": "",
            "WHATSAPP_ACCESS_TOKEN": "",
            "WHATSAPP_VERIFY_TOKEN": "",
        }
    )
    @patch.dict("os.environ", {"DJANGO_SETTINGS_MODULE": "novena_hub.settings"})
    def test_command_fails_known_production_blockers(self):
        with _patch_external_checks(), self.assertRaises(CommandError):
            call_command("production_readiness_check", stdout=io.StringIO())

        results = readiness.build_checks()
        failed_names = {result.name for result in results if result.status == "FAIL"}
        self.assertIn("DEBUG disabled", failed_names)
        self.assertIn("SECRET_KEY", failed_names)
        self.assertIn("Gateway claim secret", failed_names)
        self.assertIn("Gateway activation encryption key", failed_names)
        self.assertIn("Allowed hosts", failed_names)
        self.assertIn("Health check token", failed_names)
        self.assertIn("Amazon SES", failed_names)
        self.assertIn("Signup email verification", failed_names)
        self.assertIn("WhatsApp", failed_names)

    @override_settings(**{**PRODUCTION_SETTINGS, "MQTT_ACCEPT_LEGACY_SHARED_INBOUND": True})
    @patch.dict("os.environ", {"DJANGO_SETTINGS_MODULE": "novena_hub.settings_production"})
    def test_command_fails_when_legacy_shared_mqtt_bridge_is_enabled(self):
        with _patch_external_checks(), self.assertRaises(CommandError):
            call_command("production_readiness_check", stdout=io.StringIO())

        results = readiness.build_checks()
        failed_names = {result.name for result in results if result.status == "FAIL"}
        self.assertIn("Legacy shared MQTT inbound bridge", failed_names)

    @override_settings(**PRODUCTION_SETTINGS)
    @patch.dict("os.environ", {"DJANGO_SETTINGS_MODULE": "novena_hub.settings_production"})
    def test_command_fails_when_gateway_role_has_shared_inbound_publish_acl(self):
        unsafe_dynsec = {
            "roles": [
                {
                    "roleName": "gateway",
                    "acls": [
                        {"acltype": "publishClientSend", "topic": "v1/gateway/telemetry", "allow": True},
                    ],
                }
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as dynsec_file:
            json.dump(unsafe_dynsec, dynsec_file)
            dynsec_file.flush()

            with (
                _patch_external_checks(),
                patch.object(readiness, "_candidate_dynsec_files", return_value=[Path(dynsec_file.name)]),
                self.assertRaises(CommandError),
            ):
                call_command("production_readiness_check", stdout=io.StringIO())

            with patch.object(readiness, "_candidate_dynsec_files", return_value=[Path(dynsec_file.name)]):
                results = readiness.build_checks()

        failed_names = {result.name for result in results if result.status == "FAIL"}
        self.assertIn("Gateway MQTT inbound ACLs", failed_names)


def test_production_env_example_contains_readiness_variables():
    env_example = Path(__file__).resolve().parents[3] / "deploy" / "env" / "production.env.example"
    content = env_example.read_text()
    required = [
        "DJANGO_SETTINGS_MODULE",
        "SECRET_KEY",
        "ALLOWED_HOSTS",
        "APP_BASE_URL",
        "USE_HTTPS_IN_ABSOLUTE_URLS",
        "CSRF_TRUSTED_ORIGINS",
        "DATABASE_URL",
        "REDIS_URL",
        "MQTT_BROKER_HOST",
        "MQTT_DYNSEC_ADMIN_USER",
        "MQTT_DYNSEC_ADMIN_PASS",
        "MQTT_ACCEPT_LEGACY_SHARED_INBOUND",
        "GATEWAY_CLAIM_SECRET",
        "GATEWAY_ACTIVATION_ENCRYPTION_KEY",
        "PUBLIC_MQTT_BROKER_HOST",
        "AWS_SES_ACCESS_KEY_ID",
        "AWS_SES_SECRET_ACCESS_KEY",
        "ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED",
        "WHATSAPP_PROVIDER",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_VERIFY_TOKEN",
        "REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID",
        "REMOTE_CONTROL_SIGNING_KEYS",
        "REMOTE_CONTROL_AUDIT_RETENTION_DAYS",
        "HEALTH_CHECK_TOKENS",
        "DJSTRIPE_WEBHOOK_SECRET",
        "SENTRY_DSN",
    ]
    missing = [name for name in required if f"{name}=" not in content]

    assert missing == []
