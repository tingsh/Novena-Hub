from copy import copy

from django.conf import settings
from django.urls import reverse

from .meta import absolute_url, get_server_root


def project_meta(request):
    # modify these values as needed and add whatever else you want globally available here
    project_data = copy(settings.PROJECT_METADATA)
    project_data["TITLE"] = "{} | {}".format(project_data["NAME"], project_data["DESCRIPTION"])

    # Dynamic login/signup URLs for subdomain separation in production
    app_domain = getattr(settings, "APP_DOMAIN", None)
    if app_domain and not settings.DEBUG:
        protocol = "https" if getattr(settings, "USE_HTTPS_IN_ABSOLUTE_URLS", False) else "http"
        login_url = f"{protocol}://{app_domain}/accounts/login/"
        signup_url = f"{protocol}://{app_domain}/accounts/signup/"
    else:
        login_url = reverse("account_login")
        signup_url = reverse("account_signup")

    if settings.DEBUG:
        marketing_origin = f"{request.scheme}://{request.get_host()}"
    else:
        marketing_origin = f"https://{settings.MARKETING_SITE_DOMAIN}"
    marketing_page_url = f"{marketing_origin}{request.path}"

    from apps.subscriptions.metadata import ACTIVE_PRODUCTS

    return {
        "project_meta": project_data,
        "server_url": get_server_root(),
        "page_url": absolute_url(request.path),
        "page_title": "",
        "page_description": "",
        "page_image": "",
        "light_theme": settings.LIGHT_THEME,
        "dark_theme": settings.DARK_THEME,
        "current_theme": request.COOKIES.get("theme", ""),
        "dark_mode": request.COOKIES.get("theme", "") == settings.DARK_THEME,
        "turnstile_key": getattr(settings, "TURNSTILE_KEY", None),
        "use_i18n": getattr(settings, "USE_I18N", False) and len(getattr(settings, "LANGUAGES", [])) > 1,
        "login_url": login_url,
        "signup_url": signup_url,
        "marketing_origin": marketing_origin,
        "marketing_page_url": marketing_page_url,
        "active_products": ACTIVE_PRODUCTS,
        "public_mqtt_broker": {
            "scheme": settings.PUBLIC_MQTT_BROKER_SCHEME,
            "host": settings.PUBLIC_MQTT_BROKER_HOST,
            "port": settings.PUBLIC_MQTT_BROKER_PORT,
            "url": f"{settings.PUBLIC_MQTT_BROKER_SCHEME}://"
            f"{settings.PUBLIC_MQTT_BROKER_HOST}:{settings.PUBLIC_MQTT_BROKER_PORT}",
        },
    }


def google_analytics_id(request):
    """
    Adds google analytics id to all requests
    """
    if settings.GOOGLE_ANALYTICS_ID:
        return {
            "GOOGLE_ANALYTICS_ID": settings.GOOGLE_ANALYTICS_ID,
        }
    else:
        return {}
