import logging

import requests
from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from apps.events.models import EmailDelivery
from apps.events.services import TrackedEmailDeliveryError, record_no_email_recipients, send_tracked_email
from apps.utils.timezones import format_site_datetime

from .models import Alert
from .whatsapp import send_whatsapp_template_message, send_whatsapp_text_message

logger = logging.getLogger("novena_hub")


def _get_whatsapp_alert_template_parameters(alert, is_resolved, template_name):
    if template_name == "hello_world":
        return [], []

    if template_name == "novena_alert_notification":
        if is_resolved:
            return None
        return [
            alert.rule.severity.title(),
        ], [
            alert.rule.name,
            alert.device.name,
            str(alert.trigger_value),
            format_site_datetime(alert.triggered_at, alert.device.site),
        ]

    status_text = "RESOLVED" if is_resolved else alert.rule.severity.upper()
    return [], [
        status_text,
        alert.rule.name,
        alert.device.name,
        str(alert.trigger_value),
        alert.rule.severity.upper(),
        format_site_datetime(alert.triggered_at, alert.device.site),
        f"{settings.PROJECT_METADATA['URL']}/a/{alert.team.slug}/alerts/",
    ]


def _retry_countdown(retries):
    return min(300, 30 * (2**retries))


@shared_task(bind=True, max_retries=3)
def dispatch_alert_email_task(self, alert_id, is_resolved=False):
    try:
        alert = Alert.objects.select_related("team", "device", "device__site", "rule").get(id=alert_id)
    except Alert.DoesNotExist:
        logger.error(f"Alert {alert_id} not found for email dispatch.")
        return

    recipients = list(alert.rule.recipients.exclude(email=""))
    recipient_list = [member.email for member in recipients if member.email]
    if not recipient_list:
        notification_type = (
            EmailDelivery.NotificationType.ALERT_RESOLVED
            if is_resolved
            else EmailDelivery.NotificationType.ALERT_TRIGGERED
        )
        record_no_email_recipients(
            team=alert.team,
            notification_type=notification_type,
            alert=alert,
            metadata={"alert_id": alert.id, "is_resolved": is_resolved},
        )
        logger.warning(f"No email recipients configured for alert {alert.id}")
        return

    if is_resolved:
        subject = f"[RESOLVED] Novena Alert: {alert.rule.name}"
        notification_type = EmailDelivery.NotificationType.ALERT_RESOLVED
    else:
        subject = f"[{alert.rule.severity.upper()}] Novena Alert: {alert.rule.name}"
        notification_type = EmailDelivery.NotificationType.ALERT_TRIGGERED

    notification_timestamp = (alert.resolved_at or timezone.now()) if is_resolved else alert.triggered_at
    recovery_reading_available = is_resolved and alert.resolved_value is not None
    context = {
        "alert": alert,
        "rule": alert.rule,
        "device": alert.device,
        "site": alert.device.site,
        "timestamp": notification_timestamp,
        "timestamp_display": format_site_datetime(notification_timestamp, alert.device.site),
        "dashboard_url": f"{settings.PROJECT_METADATA['URL']}/a/{alert.team.slug}/alerts/",
        "is_resolved": is_resolved,
        "value_label": "Recovery Value" if recovery_reading_available else "Trigger Value",
        "notification_value": alert.resolved_value if recovery_reading_available else alert.trigger_value,
    }

    html_message = render_to_string("alerts/email/alert_notification.html", context)
    plain_message = render_to_string("alerts/email/alert_notification.txt", context)

    user_by_email = {member.email.lower(): member for member in recipients if member.email}
    try:
        send_tracked_email(
            team=alert.team,
            notification_type=notification_type,
            subject=subject,
            text_body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipients=recipient_list,
            html_body=html_message,
            alert=alert,
            user_by_email=user_by_email,
            metadata={"alert_id": alert.id, "is_resolved": is_resolved},
        )
        logger.info(f"Alert email sent asynchronously to {len(recipient_list)} recipients (is_resolved={is_resolved}).")
    except TrackedEmailDeliveryError as exc:
        logger.warning(
            "Alert email delivery failed for alert %s on attempt %s/%s.",
            alert.id,
            self.request.retries + 1,
            self.max_retries + 1,
        )
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc


@shared_task
def dispatch_alert_whatsapp_task(alert_id, is_resolved=False):
    try:
        alert = Alert.objects.select_related("team", "device", "device__site", "rule").get(id=alert_id)
    except Alert.DoesNotExist:
        logger.error(f"Alert {alert_id} not found for WhatsApp dispatch.")
        return

    phone_numbers = [m.phone_number for m in alert.rule.recipients.all() if m.phone_number]
    if not phone_numbers:
        logger.warning(f"No WhatsApp recipients configured for alert {alert.id}")
        return

    status_text = "RESOLVED" if is_resolved else alert.rule.severity.upper()
    emoji = "✅" if is_resolved else "🚨"
    message_text = (
        f"{emoji} *NOVENA ALERT {status_text}*\n"
        f"Rule: {alert.rule.name}\n"
        f"Device: {alert.device.name}\n"
        f"Value: {alert.trigger_value}\n"
        f"Severity: {alert.rule.severity.upper()}\n"
        f"Time: {format_site_datetime(alert.triggered_at, alert.device.site, '%H:%M:%S %Z')}"
    )

    template_name = getattr(settings, "WHATSAPP_ALERT_TEMPLATE_NAME", "hello_world")
    template_language = getattr(settings, "WHATSAPP_ALERT_TEMPLATE_LANGUAGE", "en_US")
    template_parameters = _get_whatsapp_alert_template_parameters(alert, is_resolved, template_name)
    if template_parameters is None:
        logger.info(
            "WhatsApp resolved notification skipped for alert %s because template %s is trigger-only.",
            alert.id,
            template_name,
        )
        return
    header_parameters, body_parameters = template_parameters

    for number in phone_numbers:
        sent = send_whatsapp_template_message(
            number,
            template_name,
            template_language,
            body_parameters=body_parameters,
            header_parameters=header_parameters,
        )
        if sent:
            logger.info(
                "WhatsApp alert template dispatched for alert %s (is_resolved=%s, template=%s). Alert summary: %s",
                alert.id,
                is_resolved,
                template_name,
                message_text.replace("\n", " | "),
            )


@shared_task
def dispatch_alert_webhook_task(alert_id, is_resolved=False):
    try:
        alert = Alert.objects.get(id=alert_id)
    except Alert.DoesNotExist:
        logger.error(f"Alert {alert_id} not found for Webhook dispatch.")
        return

    url = alert.rule.notify_webhook
    if not url:
        logger.warning(f"No Webhook URL configured for alert {alert.id}")
        return

    payload = {
        "alert_id": alert.id,
        "rule_name": alert.rule.name,
        "severity": alert.rule.severity,
        "device": {"id": alert.device.id, "name": alert.device.name},
        "site": {"id": alert.device.site.id, "name": alert.device.site.name},
        "trigger_value": alert.trigger_value,
        "condition": alert.rule.condition,
        "threshold": alert.rule.threshold,
        "timestamp": alert.triggered_at.isoformat(),
        "status": "resolved" if is_resolved else alert.status,
    }

    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        logger.info(f"Alert Webhook successfully dispatched to {url} (is_resolved={is_resolved})")
    except Exception as e:
        logger.error(f"Alert Webhook dispatch failed for alert {alert.id}: {e}")


@shared_task
def send_whatsapp_message_task(phone_number, message_text):
    """
    Sends a general WhatsApp text message using Meta API or mock logs.
    """
    send_whatsapp_text_message(phone_number, message_text)
