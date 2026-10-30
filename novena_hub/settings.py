"""
Django settings for Novena Hub.

For more information on this file, see
https://docs.djangoproject.com/en/stable/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/stable/ref/settings/
"""

import os
import sys
from pathlib import Path

import environ
from corsheaders.defaults import default_headers
from django.utils.translation import gettext_lazy

# Build paths inside the project like this: BASE_DIR / "subdir".
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
env.read_env(os.path.join(BASE_DIR, ".env"))

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/stable/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env("SECRET_KEY", default="django-insecure-yGgc2F6ItlonpCKbwDYLENz2r1DAIAWzZ343C3tA")

# SECURITY WARNING: don"t run with debug turned on in production!
DEBUG = env.bool("DEBUG", default=True)
ENABLE_DEBUG_TOOLBAR = env.bool("ENABLE_DEBUG_TOOLBAR", default=False) and "test" not in sys.argv

# Note: It is not recommended to set ALLOWED_HOSTS to "*" in production
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])


# Application definition

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sitemaps",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.forms",
]

# Put your third-party apps here
THIRD_PARTY_APPS = [
    "allauth",  # allauth account/registration management
    "allauth.account",
    "allauth.headless",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "channels",
    "django_htmx",
    "django_vite",
    "allauth.mfa",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "drf_spectacular",
    "rest_framework_api_key",
    "celery_progress",
    "hijack",  # "login as" functionality
    "hijack.contrib.admin",  # hijack buttons in the admin
    "djstripe",  # stripe integration
    "waffle",
    "health_check",
    "health_check.db",
    "health_check.contrib.celery",
    "health_check.contrib.redis",
    "django_celery_beat",
    "template_partials.apps.SimpleAppConfig",
]

WAGTAIL_APPS = [
    "wagtail.contrib.forms",
    "wagtail.contrib.redirects",
    "wagtail.contrib.simple_translation",
    "wagtail.embeds",
    "wagtail.sites",
    "wagtail.users",
    "wagtail.snippets",
    "wagtail.documents",
    "wagtail.images",
    "wagtail.locales",
    "wagtail.search",
    "wagtail.admin",
    "wagtail",
    "modelcluster",
    "taggit",
]

# Put your project-specific apps here
PROJECT_APPS = [
    "apps.content",
    "apps.subscriptions.apps.SubscriptionConfig",
    "apps.users.apps.UserConfig",
    "apps.dashboard.apps.DashboardConfig",
    "apps.api.apps.APIConfig",
    "apps.utils",
    "apps.web",
    "apps.teams.apps.TeamConfig",
    "apps.chat",
    "apps.devices",
    "apps.telemetry",
    "apps.alerts",
    "apps.onboarding",
    "apps.events",
    "apps.maintenance",
    "apps.automations",
    "apps.impact.apps.ImpactConfig",
]

# MQTT Settings
MQTT_BROKER_HOST = env("MQTT_BROKER_HOST", default="localhost")
MQTT_BROKER_PORT = env.int("MQTT_BROKER_PORT", default=1883)
MQTT_CONSUMER_CLIENT_ID = env("MQTT_CONSUMER_CLIENT_ID", default="novena-hub-consumer")
MQTT_PUBLISHER_CLIENT_ID = env("MQTT_PUBLISHER_CLIENT_ID", default="novena-hub-publisher")

# Mosquitto Dynamic Security Plugin
MQTT_DYNSEC_PORT = env.int("MQTT_DYNSEC_PORT", default=1884)
MQTT_DYNSEC_ADMIN_USER = env("MQTT_DYNSEC_ADMIN_USER", default="dynsec-admin")
MQTT_DYNSEC_ADMIN_PASS = env("MQTT_DYNSEC_ADMIN_PASS", default="dynsec-password")
MQTT_PROVISIONING_REQUIRED = env.bool("MQTT_PROVISIONING_REQUIRED", default=False)
MQTT_ACCEPT_LEGACY_SHARED_INBOUND = env.bool("MQTT_ACCEPT_LEGACY_SHARED_INBOUND", default=False)
REMOTE_CONTROL_SIGNING_KEY_ID = env("REMOTE_CONTROL_SIGNING_KEY_ID", default="unconfigured")
REMOTE_CONTROL_SIGNING_PRIVATE_KEY = env("REMOTE_CONTROL_SIGNING_PRIVATE_KEY", default="")
REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID = env(
    "REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID",
    default=REMOTE_CONTROL_SIGNING_KEY_ID,
)
REMOTE_CONTROL_SIGNING_KEYS = env.json("REMOTE_CONTROL_SIGNING_KEYS", default={})
REMOTE_CONTROL_AUDIT_RETENTION_DAYS = env.int("REMOTE_CONTROL_AUDIT_RETENTION_DAYS", default=2555)
NOVENA_DEPLOYMENT_MODE = env("NOVENA_DEPLOYMENT_MODE", default="local")
PUBLIC_MQTT_BROKER_SCHEME = env("PUBLIC_MQTT_BROKER_SCHEME", default="mqtt")
PUBLIC_MQTT_BROKER_HOST = env("PUBLIC_MQTT_BROKER_HOST", default=MQTT_BROKER_HOST)
PUBLIC_MQTT_BROKER_PORT = env.int("PUBLIC_MQTT_BROKER_PORT", default=MQTT_BROKER_PORT)

# Gateway Claim Code (HMAC secret for deriving claim codes from serial numbers)
GATEWAY_CLAIM_SECRET = env("GATEWAY_CLAIM_SECRET", default="change-me-in-production")
GATEWAY_ACTIVATION_ENCRYPTION_KEY = env("GATEWAY_ACTIVATION_ENCRYPTION_KEY", default="")
GATEWAY_ACTIVATION_TTL_HOURS = env.int("GATEWAY_ACTIVATION_TTL_HOURS", default=24)
GATEWAY_ACTIVATION_REISSUE_COOLDOWN_SECONDS = env.int("GATEWAY_ACTIVATION_REISSUE_COOLDOWN_SECONDS", default=300)
MQTT_DYNSEC_RESPONSE_TIMEOUT_SECONDS = env.int("MQTT_DYNSEC_RESPONSE_TIMEOUT_SECONDS", default=5)
GATEWAY_RELEASE_LEASE_SECONDS = env.int("GATEWAY_RELEASE_LEASE_SECONDS", default=60)
GATEWAY_RELEASE_RETRY_MAX_SECONDS = env.int("GATEWAY_RELEASE_RETRY_MAX_SECONDS", default=300)

# Hardware health and telemetry freshness thresholds.
DEVICE_OFFLINE_MIN_SECONDS = env.int("DEVICE_OFFLINE_MIN_SECONDS", default=30)
DEVICE_OFFLINE_MULTIPLIER = env.float("DEVICE_OFFLINE_MULTIPLIER", default=3)
DEVICE_DELAYED_MULTIPLIER = env.float("DEVICE_DELAYED_MULTIPLIER", default=2)
GATEWAY_OFFLINE_SECONDS = env.int("GATEWAY_OFFLINE_SECONDS", default=120)

# WhatsApp Settings
WHATSAPP_PROVIDER = env("WHATSAPP_PROVIDER", default="mock")  # 'mock' or 'meta'
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + PROJECT_APPS + WAGTAIL_APPS

if DEBUG:
    # in debug mode, add daphne to the beginning of INSTALLED_APPS to enable async support
    INSTALLED_APPS.insert(0, "daphne")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.teams.middleware.TeamsMiddleware",
    "apps.teams.middleware.SiteBoundaryMiddleware",
    "apps.web.middleware.locale.UserLocaleMiddleware",
    "apps.web.middleware.locale.UserTimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "wagtail.contrib.redirects.middleware.RedirectMiddleware",
    "hijack.middleware.HijackUserMiddleware",
    "waffle.middleware.WaffleMiddleware",
]

if ENABLE_DEBUG_TOOLBAR:
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
    INSTALLED_APPS.append("debug_toolbar")
    INTERNAL_IPS = ["127.0.0.1"]
    try:
        import socket

        # get hostname for Docker environments
        # See https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#configure-internal-ips
        hostname, _, ips = socket.gethostbyname_ex(socket.gethostname())
        # add discovered IPs plus some common defaults
        INTERNAL_IPS += [ip[: ip.rfind(".")] + ".1" for ip in ips] + ["192.168.65.1", "10.0.2.2"]
    except OSError as e:
        print(f"{e} while attempting to resolve system hostname. Using INTERNAL_IPS={INTERNAL_IPS}")

ROOT_URLCONF = "novena_hub.urls"


# used to disable the cache in dev, but turn it on in production.
# more here: https://nickjanetakis.com/blog/django-4-1-html-templates-are-cached-by-default-with-debug-true
_LOW_LEVEL_LOADERS = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]

# Manually load template partials to allow for easier integration with other templating systems
# like django-cotton.
# https://github.com/carltongibson/django-template-partials?tab=readme-ov-file#advanced-configuration

_DEFAULT_LOADERS = [
    (
        "template_partials.loader.Loader",
        _LOW_LEVEL_LOADERS,
    ),
]

_CACHED_LOADERS = [
    (
        "template_partials.loader.Loader",
        [
            ("django.template.loaders.cached.Loader", _LOW_LEVEL_LOADERS),
        ],
    ),
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
        ],
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context_processors.project_meta",
                "apps.teams.context_processors.team",
                "apps.teams.context_processors.user_teams",
                # this line can be removed if not using google analytics
                "apps.web.context_processors.google_analytics_id",
            ],
            "loaders": _DEFAULT_LOADERS if DEBUG else _CACHED_LOADERS,
            "builtins": [
                "template_partials.templatetags.partials",
            ],
        },
    },
]

WSGI_APPLICATION = "novena_hub.wsgi.application"

FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

# Database
# https://docs.djangoproject.com/en/stable/ref/settings/#databases

if "DATABASE_URL" in env:
    DATABASES = {"default": env.db()}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DJANGO_DATABASE_NAME", default="novena_hub"),
            "USER": env("DJANGO_DATABASE_USER", default="postgres"),
            "PASSWORD": env("DJANGO_DATABASE_PASSWORD", default="***"),
            "HOST": env("DJANGO_DATABASE_HOST", default="localhost"),
            "PORT": env("DJANGO_DATABASE_PORT", default="5432"),
        }
    }

if "test" in sys.argv or env.bool("USING_TEST_DB", default=False):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }


# Auth and Login

# Django recommends overriding the user model even if you don"t think you need to because it makes
# future changes much easier.
AUTH_USER_MODEL = "users.CustomUser"
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "/"

# Domain and Subdomain settings for production routing
APP_DOMAIN = env("APP_DOMAIN", default=None)
MARKETING_SITE_DOMAIN = env("MARKETING_SITE_DOMAIN", default="www.novenaplatform.com")
SESSION_COOKIE_DOMAIN = env("SESSION_COOKIE_DOMAIN", default=None)

# Password validation
# https://docs.djangoproject.com/en/stable/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Allauth setup

ACCOUNT_ADAPTER = "apps.teams.adapter.AcceptInvitationAdapter"
HEADLESS_ADAPTER = "apps.users.adapter.CustomHeadlessAdapter"
ACCOUNT_AUTHENTICATION_METHOD = "email"
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*"]

ACCOUNT_EMAIL_SUBJECT_PREFIX = ""
ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS = False  # don't send "forgot password" emails to unknown accounts
ACCOUNT_CONFIRM_EMAIL_ON_GET = True
ACCOUNT_UNIQUE_EMAIL = True
# This configures a honeypot field to prevent bots from signing up.
# The ID strikes a balance of "realistic" - to catch bots,
# and "not too common" - to not trip auto-complete in browsers.
# You can change the ID or remove it entirely to disable the honeypot.
ACCOUNT_SIGNUP_FORM_HONEYPOT_FIELD = "phone_number_x"
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_LOGOUT_ON_GET = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_USER_DISPLAY = lambda user: user.get_display_name()  # noqa: E731

ACCOUNT_FORMS = {
    "signup": "apps.teams.forms.TeamSignupForm",
}
SOCIALACCOUNT_FORMS = {
    "signup": "apps.users.forms.CustomSocialSignupForm",
}

FRONTEND_ADDRESS = env("FRONTEND_ADDRESS", default="http://localhost:5173")
USE_HEADLESS_URLS = env.bool("USE_HEADLESS_URLS", default=False)
if USE_HEADLESS_URLS:
    # These URLs will use the React front end instead of the Django views
    HEADLESS_FRONTEND_URLS = {
        "account_confirm_email": f"{FRONTEND_ADDRESS}/account/verify-email/{{key}}",
        "account_reset_password_from_key": f"{FRONTEND_ADDRESS}/account/password/reset/key/{{key}}",
        "account_signup": f"{FRONTEND_ADDRESS}/account/signup",
    }

# needed for cross-origin CSRF
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[FRONTEND_ADDRESS])
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = (*default_headers, "x-password-reset-key", "x-email-verification-key")

# User signup configuration: change to "mandatory" to require users to confirm email before signing in.
# or "optional" to send confirmation emails but not require them
ACCOUNT_EMAIL_VERIFICATION = env("ACCOUNT_EMAIL_VERIFICATION", default="none")

AUTHENTICATION_BACKENDS = (
    # Needed to login by username in Django admin, regardless of `allauth`
    "django.contrib.auth.backends.ModelBackend",
    # `allauth` specific authentication methods, such as login by e-mail
    "allauth.account.auth_backends.AuthenticationBackend",
)

# enable social login
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APPS": [
            {
                "client_id": env("GOOGLE_CLIENT_ID", default=""),
                "secret": env("GOOGLE_SECRET_ID", default=""),
                "key": "",
            },
        ],
        "SCOPE": [
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
    },
}

# For turnstile captchas
TURNSTILE_KEY = env("TURNSTILE_KEY", default=None)
TURNSTILE_SECRET = env("TURNSTILE_SECRET", default=None)


# Internationalization
# https://docs.djangoproject.com/en/stable/topics/i18n/

LANGUAGE_CODE = "en-us"
LANGUAGE_COOKIE_NAME = "novena_hub_language"
LANGUAGES = WAGTAIL_CONTENT_LANGUAGES = [
    ("en", gettext_lazy("English")),
    ("fr", gettext_lazy("French")),
]
LOCALE_PATHS = (BASE_DIR / "locale",)

TIME_ZONE = "UTC"

USE_I18N = WAGTAIL_I18N_ENABLED = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/stable/howto/static-files/

STATIC_ROOT = BASE_DIR / "static_root"
STATIC_URL = "/static/"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        # swap these to use manifest storage to bust cache when files change
        # note: this may break image references in sass/css files which is why it is not enabled by default
        # "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"
PRIVATE_MEDIA_ROOT = BASE_DIR / "private_media"
STORAGES["impact_reports"] = {
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {
        "location": PRIVATE_MEDIA_ROOT,
        "base_url": None,
    },
}
STORAGES["template_request_documents"] = {
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {"location": PRIVATE_MEDIA_ROOT / "template_requests", "base_url": None},
}

USE_S3_MEDIA = env.bool("USE_S3_MEDIA", default=False)
if USE_S3_MEDIA:
    # Media file storage in S3
    # Using this will require configuration of the S3 bucket
    # See https://docs.saaspegasus.com/configuration/#storing-media-files
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="novena_hub-media")
    AWS_S3_CUSTOM_DOMAIN = f"{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com"
    PUBLIC_MEDIA_LOCATION = "media"
    MEDIA_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/{PUBLIC_MEDIA_LOCATION}/"
    STORAGES["default"] = {
        "BACKEND": "apps.web.storage_backends.PublicMediaStorage",
    }
    STORAGES["impact_reports"] = {
        "BACKEND": "apps.web.storage_backends.PrivateMediaStorage",
    }
    STORAGES["template_request_documents"] = {
        "BACKEND": "apps.web.storage_backends.PrivateMediaStorage",
        "OPTIONS": {"location": "private/template_requests"},
    }

# Vite Integration
DJANGO_VITE = {
    "default": {
        "dev_mode": env.bool("DJANGO_VITE_DEV_MODE", default=DEBUG),
        "manifest_path": BASE_DIR / "static" / ".vite" / "manifest.json",
    }
}

# Default primary key field type
# https://docs.djangoproject.com/en/stable/ref/settings/#default-auto-field

# future versions of Django will use BigAutoField as the default, but it can result in unwanted library
# migration files being generated, so we stick with AutoField for now.
# change this to BigAutoField if you"re sure you want to use it and aren"t worried about migrations.
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Removes deprecation warning for future compatibility.
# see https://adamj.eu/tech/2023/12/07/django-fix-urlfield-assume-scheme-warnings/ for details.
FORMS_URLFIELD_ASSUME_HTTPS = True

# Email setup

# default email used by your server
SERVER_EMAIL = env("SERVER_EMAIL", default="noreply@localhost:8000")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="tingshouheng@gmail.com")

# ANYMAIL Configuration for Amazon SES
AWS_SES_REGION_NAME = env("AWS_SES_REGION_NAME", default="ap-southeast-1")
AWS_SES_ACCESS_KEY_ID = env("AWS_SES_ACCESS_KEY_ID", default=None)
AWS_SES_SECRET_ACCESS_KEY = env("AWS_SES_SECRET_ACCESS_KEY", default=None)
AWS_SES_CONFIGURATION_SET_NAME = env("AWS_SES_CONFIGURATION_SET_NAME", default=None) or None
ANYMAIL_WEBHOOK_SECRET = env("ANYMAIL_WEBHOOK_SECRET", default="")
AWS_SES_CLIENT_PARAMS = {
    "region_name": AWS_SES_REGION_NAME,
}
if AWS_SES_ACCESS_KEY_ID and AWS_SES_SECRET_ACCESS_KEY:
    AWS_SES_CLIENT_PARAMS.update(
        {
            "aws_access_key_id": AWS_SES_ACCESS_KEY_ID,
            "aws_secret_access_key": AWS_SES_SECRET_ACCESS_KEY,
        }
    )

ANYMAIL = {
    "AMAZON_SES_CLIENT_PARAMS": AWS_SES_CLIENT_PARAMS,
    "AMAZON_SES_CONFIGURATION_SET_NAME": AWS_SES_CONFIGURATION_SET_NAME,
    "WEBHOOK_SECRET": ANYMAIL_WEBHOOK_SECRET,
}
EMAIL_BACKEND = env("EMAIL_BACKEND", default="anymail.backends.amazon_ses.EmailBackend")

# WhatsApp Meta API Configuration
WHATSAPP_GRAPH_API_VERSION = env("WHATSAPP_GRAPH_API_VERSION", default="v21.0")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default=None)
WHATSAPP_ACCESS_TOKEN = env("WHATSAPP_ACCESS_TOKEN", default=None)
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN", default="")
WHATSAPP_ALERT_TEMPLATE_NAME = env("WHATSAPP_ALERT_TEMPLATE_NAME", default="hello_world")
WHATSAPP_ALERT_TEMPLATE_LANGUAGE = env("WHATSAPP_ALERT_TEMPLATE_LANGUAGE", default="en_US")

EMAIL_SUBJECT_PREFIX = "[Novena] "

# Django sites

SITE_ID = 1

# DRF config
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ("apps.api.permissions.IsAuthenticatedOrHasUserAPIKey",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 100,
}

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"])


SPECTACULAR_SETTINGS = {
    "TITLE": "Novena Platform API",
    "DESCRIPTION": "Novena Platform industrial IoT API",  # noqa: E501
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_SETTINGS": {
        "displayOperationId": True,
    },
    "PREPROCESSING_HOOKS": [
        "apps.api.schema.filter_schema_apis",
    ],
    "APPEND_COMPONENTS": {
        "securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "Authorization"}}
    },
    "SECURITY": [
        {
            "ApiKeyAuth": [],
        }
    ],
}

# Redis, cache, and/or Celery setup
if "REDIS_URL" in env:
    REDIS_URL = env("REDIS_URL")
elif "REDIS_TLS_URL" in env:
    REDIS_URL = env("REDIS_TLS_URL")
else:
    REDIS_HOST = env("REDIS_HOST", default="localhost")
    REDIS_PORT = env("REDIS_PORT", default="6379")
    REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

if REDIS_URL.startswith("rediss"):
    REDIS_URL = f"{REDIS_URL}?ssl_cert_reqs=none"

DUMMY_CACHE = {
    "BACKEND": "django.core.cache.backends.dummy.DummyCache",
}
REDIS_CACHE = {
    "BACKEND": "django.core.cache.backends.redis.RedisCache",
    "LOCATION": REDIS_URL,
}
CACHES = {
    "default": DUMMY_CACHE if DEBUG else REDIS_CACHE,
}

CELERY_BROKER_URL = CELERY_RESULT_BACKEND = REDIS_URL
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "dispatch-due-gateway-config-outboxes": {
        "task": "apps.devices.tasks.dispatch_due_gateway_config_outboxes",
        "schedule": 30.0,
    },
    "dispatch-due-gateway-releases": {
        "task": "apps.devices.tasks.dispatch_due_gateway_releases",
        "schedule": 30.0,
    },
    "dispatch-due-plan-reconciliations": {
        "task": "apps.devices.tasks.dispatch_due_plan_reconciliations",
        "schedule": 60.0,
    },
    "dispatch-due-remote-command-outboxes": {
        "task": "apps.devices.tasks.dispatch_due_remote_command_outboxes",
        "schedule": 5.0,
    },
    "generate-preventive-tickets": {
        "task": "apps.maintenance.tasks.generate_preventive_tickets",
        "schedule": 86400.0,  # Run daily
    },
    "flush-telemetry-buffer": {
        "task": "apps.telemetry.tasks.flush_telemetry_buffer_task",
        "schedule": 2.0,  # Run every 2 seconds
    },
    "flush-logs-buffer": {
        "task": "apps.telemetry.tasks.flush_logs_buffer_task",
        "schedule": 2.0,  # Run every 2 seconds
    },
    "check-device-heartbeats": {
        "task": "apps.devices.tasks.check_device_heartbeats",
        "schedule": 10.0,
    },
    "check-gateway-heartbeats": {
        "task": "apps.devices.tasks.check_gateway_heartbeats",
        "schedule": 30.0,
    },
    "expire-and-retry-gateway-activations": {
        "task": "apps.devices.tasks.expire_and_retry_gateway_activations",
        "schedule": 60.0,
    },
    "expire-control-activations": {
        "task": "apps.devices.tasks.expire_control_activations",
        "schedule": 3600.0,
    },
    "refresh-business-impact": {
        "task": "apps.impact.tasks.dispatch_impact_refreshes",
        "schedule": 3600.0,
    },
    "finalize-daily-business-impact": {
        "task": "apps.impact.tasks.dispatch_daily_impact_finalization",
        "schedule": 3600.0,
    },
    "generate-monthly-business-impact-reports": {
        "task": "apps.impact.tasks.dispatch_monthly_impact_reports",
        "schedule": 21600.0,
    },
    "cleanup-business-impact-history": {
        "task": "apps.impact.tasks.cleanup_impact_history",
        "schedule": 86400.0,
    },
}

REMOTE_CONTROL_OUTBOX_LEASE_SECONDS = env.int("REMOTE_CONTROL_OUTBOX_LEASE_SECONDS", default=60)
REMOTE_CONTROL_OUTBOX_MAX_ATTEMPTS = env.int("REMOTE_CONTROL_OUTBOX_MAX_ATTEMPTS", default=5)
REMOTE_CONTROL_OUTBOX_RETRY_BASE_SECONDS = env.int(
    "REMOTE_CONTROL_OUTBOX_RETRY_BASE_SECONDS",
    default=5,
)
REMOTE_CONTROL_OUTBOX_RETRY_MAX_SECONDS = env.int(
    "REMOTE_CONTROL_OUTBOX_RETRY_MAX_SECONDS",
    default=300,
)
GATEWAY_CONFIG_ENVELOPE_TTL_SECONDS = env.int("GATEWAY_CONFIG_ENVELOPE_TTL_SECONDS", default=300)
GATEWAY_CONFIG_OUTBOX_LEASE_SECONDS = env.int("GATEWAY_CONFIG_OUTBOX_LEASE_SECONDS", default=60)
GATEWAY_CONFIG_OUTBOX_MAX_ATTEMPTS = env.int("GATEWAY_CONFIG_OUTBOX_MAX_ATTEMPTS", default=5)
GATEWAY_CONFIG_INTENT_TTL_SECONDS = env.int("GATEWAY_CONFIG_INTENT_TTL_SECONDS", default=86400)
GATEWAY_CONFIG_APPLY_TIMEOUT_SECONDS = env.int("GATEWAY_CONFIG_APPLY_TIMEOUT_SECONDS", default=300)
GUIDED_SETUP_FIRST_TELEMETRY_TIMEOUT_SECONDS = env.int(
    "GUIDED_SETUP_FIRST_TELEMETRY_TIMEOUT_SECONDS",
    default=180,
)

# Channels / Daphne setup

ASGI_APPLICATION = "novena_hub.asgi.application"
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
        },
    },
}

# Health Checks
# A list of tokens that can be used to access the health check endpoint
HEALTH_CHECK_TOKENS = env.list("HEALTH_CHECK_TOKENS", default="")

# Wagtail config

WAGTAIL_SITE_NAME = "Novena Platform Content"
WAGTAILADMIN_BASE_URL = env("APP_BASE_URL", default="http://localhost:8000")

# Waffle config

WAFFLE_FLAG_MODEL = "teams.Flag"

# Pegasus config

# replace any values below with specifics for your project
PROJECT_METADATA = {
    "NAME": gettext_lazy("Novena Platform"),
    "URL": env("APP_BASE_URL", default="http://localhost:8000"),
    "DESCRIPTION": gettext_lazy("Industrial IoT operations platform"),  # noqa: E501
    "IMAGE": "https://upload.wikimedia.org/wikipedia/commons/2/20/PEO-pegasus_black.svg",
    "KEYWORDS": "Industrial IoT, SaaS, Novena Hub, Novena Gateway",
    "CONTACT_EMAIL": env("CONTACT_EMAIL", default="support@example.com"),
}

# set this to True in production to have URLs generated with https instead of http
USE_HTTPS_IN_ABSOLUTE_URLS = env.bool("USE_HTTPS_IN_ABSOLUTE_URLS", default=False)

ADMINS = [("Shouheng", "tingshouheng@gmail.com")]

# Add your google analytics ID to the environment to connect to Google Analytics
GOOGLE_ANALYTICS_ID = env("GOOGLE_ANALYTICS_ID", default="")

# these daisyui themes are used to set the dark and light themes for the site
# they must be valid themes included in your tailwind.config.js file.
# more here: https://daisyui.com/docs/themes/
LIGHT_THEME = "light"
DARK_THEME = "dark"

# Stripe config
# modeled to be the same as https://github.com/dj-stripe/dj-stripe
# Note: don"t edit these values here - edit them in your .env file or environment variables!
# The defaults are provided to prevent crashes if your keys don"t match the expected format.
STRIPE_LIVE_PUBLIC_KEY = env("STRIPE_LIVE_PUBLIC_KEY", default="pk_live_***")
STRIPE_LIVE_SECRET_KEY = env("STRIPE_LIVE_SECRET_KEY", default="sk_live_***")
STRIPE_TEST_PUBLIC_KEY = env("STRIPE_TEST_PUBLIC_KEY", default="pk_test_***")
STRIPE_TEST_SECRET_KEY = env("STRIPE_TEST_SECRET_KEY", default="sk_test_***")
# Change to True in production
STRIPE_LIVE_MODE = env.bool("STRIPE_LIVE_MODE", False)
STRIPE_PRICING_TABLE_ID = env("STRIPE_PRICING_TABLE_ID", default="")

# djstripe settings

DJSTRIPE_FOREIGN_KEY_TO_FIELD = "id"  # change to "djstripe_id" if not a new installation
DJSTRIPE_SUBSCRIBER_MODEL = "teams.Team"
DJSTRIPE_SUBSCRIBER_MODEL_REQUEST_CALLBACK = lambda request: request.team  # noqa E731

# Local Stripe CLI workflows may leave this unset. Production settings default
# to signature verification and can only be changed explicitly through the
# environment.
DJSTRIPE_WEBHOOK_VALIDATION = env("DJSTRIPE_WEBHOOK_VALIDATION", default=None)
DJSTRIPE_WEBHOOK_SECRET = env("DJSTRIPE_WEBHOOK_SECRET", default="")

NOVENA_OTA_SIGNING_PRIVATE_KEY = env("NOVENA_OTA_SIGNING_PRIVATE_KEY", default="")
NOVENA_OTA_SIGNING_KEY_ID = env("NOVENA_OTA_SIGNING_KEY_ID", default="novena-ota-v1")
NOVENA_OTA_MANIFEST_TTL_DAYS = env.int("NOVENA_OTA_MANIFEST_TTL_DAYS", default=14)

SILENCED_SYSTEM_CHECKS = [
    "djstripe.I002",  # Pegasus uses the same settings as dj-stripe for keys, so don't complain they are here
]

if "test" in sys.argv:
    # Silence unnecessary warnings in tests
    SILENCED_SYSTEM_CHECKS.append("djstripe.I002")


# AI Chat Setup
AI_CHAT_OPENAI_API_KEY = env("AI_CHAT_OPENAI_API_KEY", default="")
# LiteLLM models
# See:
# * https://docs.litellm.ai/docs/providers
# * https://docs.litellm.ai/docs/set_keys#litellm-variables
LLM_MODELS = {
    "gpt-3.5-turbo": {"api_key": env("AI_CHAT_OPENAI_API_KEY", default="")},
    "gpt-4o": {"api_key": env("AI_CHAT_OPENAI_API_KEY", default="")},
    "claude-3-opus-20240229": {"api_key": env("AI_CHAT_ANTHROPIC_API_KEY", default="")},
    "ollama_chat/llama3": {"api_base": env("API_CHAT_OLLAMA_API_BASE", default="http://localhost:11434")},
}
DEFAULT_LLM_MODEL = env("AI_CHAT_DEFAULT_LLM_MODEL", default="gpt-4o")
if DEFAULT_LLM_MODEL not in LLM_MODELS:
    raise ValueError(f"AI_CHAT_DEFAULT_LLM_MODEL {DEFAULT_LLM_MODEL} not found in LLM_MODELS")


# Sentry setup

# populate this to configure sentry. should take the form: "https://****@sentry.io/12345"
SENTRY_DSN = env("SENTRY_DSN", default="")


if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(dsn=SENTRY_DSN, integrations=[DjangoIntegration()])

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": '[{asctime}] {levelname} "{name}" {message}',
            "style": "{",
            "datefmt": "%d/%b/%Y %H:%M:%S",  # match Django server time format
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL", default="INFO"),
        },
        "novena_hub": {
            "handlers": ["console"],
            "level": env("NOVENA_HUB_LOG_LEVEL", default="INFO"),
        },
    },
}
