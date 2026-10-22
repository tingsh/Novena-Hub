"""Delivery of automation notifications outside the telemetry ingestion path."""

import logging

from celery import shared_task
from django.conf import settings

from apps.devices.models import Device
from apps.events.models import EmailDelivery
from apps.events.services import TrackedEmailDeliveryError, record_no_email_recipients, send_tracked_email

from .models import AutomationAction

logger = logging.getLogger("novena_hub")


@shared_task(bind=True, max_retries=3)
def dispatch_automation_email_task(self, action_id, event_id, trigger_device_id=None):
    action = AutomationAction.objects.select_related("automation", "automation__team").filter(pk=action_id).first()
    if not action or action.action_type != "notify_email":
        logger.warning("Automation email action %s no longer exists.", action_id)
        return

    automation = action.automation
    device = (
        Device.objects.filter(pk=trigger_device_id, team=automation.team).first()
        if trigger_device_id else None
    )
    recipients = [email.strip() for email in action.notify_emails.split(",") if email.strip()]
    metadata = {
        "idempotency_key": f"automation:{event_id}:action:{action.pk}",
        "automation_id": automation.pk,
        "action_id": action.pk,
        "trigger_device_id": device.pk if device else None,
    }
    if not recipients:
        record_no_email_recipients(
            team=automation.team, notification_type=EmailDelivery.NotificationType.AUTOMATION,
            metadata=metadata,
        )
        return

    subject = f"Novena Automation: {automation.name}"
    equipment = f"Equipment: {device.name}\n" if device else ""
    body = (
        f"Automation: {automation.name}\n{equipment}"
        f"Review the rule and equipment in Novena: "
        f"{settings.PROJECT_METADATA['URL']}/a/{automation.team.slug}/automations/{automation.pk}/\n"
    )
    try:
        send_tracked_email(
            team=automation.team,
            notification_type=EmailDelivery.NotificationType.AUTOMATION,
            subject=subject,
            text_body=body,
            recipients=recipients,
            from_email=settings.DEFAULT_FROM_EMAIL,
            metadata=metadata,
        )
    except TrackedEmailDeliveryError as exc:
        raise self.retry(exc=exc, countdown=min(300, 30 * 2**self.request.retries)) from exc


@shared_task(name="apps.automations.tasks.check_gateway_heartbeats")
def check_gateway_heartbeats():
    """Compatibility wrapper; gateway offline transitions are owned by apps.devices.tasks."""
    from apps.devices.tasks import check_gateway_heartbeats as devices_check_gateway_heartbeats

    return devices_check_gateway_heartbeats()
