import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import PreventiveSchedule
from .services import advance_schedule_due_date, create_pm_ticket

logger = logging.getLogger("novena_hub")


@shared_task
def generate_preventive_tickets():
    """
    Scans for active Preventive Schedules (both calendar and usage based) and generates tickets.
    """
    now = timezone.now()

    # 1. Calendar-based schedules
    calendar_schedules = PreventiveSchedule.objects.filter(is_active=True, is_usage_based=False, next_due_at__lte=now)

    for schedule in calendar_schedules:
        with transaction.atomic():
            locked = PreventiveSchedule.objects.select_for_update().get(pk=schedule.pk)
            if locked.is_active and not locked.is_usage_based and locked.next_due_at and locked.next_due_at <= now:
                create_pm_ticket(locked)
                advance_schedule_due_date(locked)

    # 2. Usage-based schedules
    from apps.telemetry.models import TelemetryData

    usage_schedules = PreventiveSchedule.objects.filter(is_active=True, is_usage_based=True)
    for schedule in usage_schedules:
        point = (
            TelemetryData.objects.filter(
                device=schedule.device,
                key=schedule.usage_telemetry_key,
                value_numeric__isnull=False,
                timestamp__gte=now - timezone.timedelta(minutes=5),
            )
            .order_by("-timestamp")
            .first()
        )
        current_val = point.value_numeric if point else None
        if current_val is not None:
            try:
                current_usage = float(current_val)
                with transaction.atomic():
                    locked = PreventiveSchedule.objects.select_for_update().get(pk=schedule.pk)
                    last_trigger = locked.last_trigger_usage_value
                    threshold = float(locked.usage_threshold or 0.0)
                    if not locked.is_active or not locked.is_usage_based or threshold <= 0 or last_trigger is None:
                        continue
                    if current_usage < last_trigger:
                        # Counter reset: establish a new baseline without creating a ticket.
                        locked.last_trigger_usage_value = current_usage
                        locked.save(update_fields=["last_trigger_usage_value"])
                    elif current_usage >= last_trigger + threshold:
                        create_pm_ticket(locked, current_usage)
                        locked.last_trigger_usage_value = current_usage
                        locked.save(update_fields=["last_trigger_usage_value"])
            except (ValueError, TypeError):
                pass
