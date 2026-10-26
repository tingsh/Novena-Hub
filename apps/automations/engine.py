import logging
from datetime import timedelta
from uuid import uuid4

import requests
from django.core.cache import cache
from django.utils import timezone

from .models import Automation, AutomationLog

logger = logging.getLogger("novena_hub")


def evaluate_automations(device, telemetry_data):
    """
    Evaluates all active automations for the given device and telemetry payload.
    Called from ingest_telemetry_data.
    """
    if not isinstance(telemetry_data, dict):
        return

    # Find automations that have conditions tied to this device
    active_automations = Automation.objects.filter(is_active=True, team=device.team).prefetch_related(
        "conditions", "actions"
    )

    for automation in active_automations:
        # Check cooldown
        if automation.last_triggered_at and timezone.now() < automation.last_triggered_at + timedelta(
            minutes=automation.cooldown_minutes
        ):
            continue

        conditions = automation.conditions.all()
        if not conditions:
            continue

        results = []
        for condition in conditions:
            # We only evaluate if the telemetry key is present in this payload OR if the condition is sustained tracking
            if condition.device == device and condition.telemetry_key in telemetry_data:
                results.append(evaluate_condition(condition, telemetry_data[condition.telemetry_key]))
            else:
                # If we don't have new data for this condition's key, assume its last state from Redis or False
                cache_key = f"auto_cond_state_{condition.id}"
                last_state = cache.get(cache_key)
                if last_state:
                    # check if duration is satisfied
                    if last_state["is_met"]:
                        results.append(check_duration(condition, last_state["met_since"]))
                    else:
                        results.append(False)
                else:
                    results.append(False)

        # Apply logic
        triggered = all(results) if automation.trigger_logic == "and" else any(results)

        if triggered:
            execute_automation(automation, trigger_device=device)


def evaluate_condition(condition, current_value):
    """Evaluates a single condition and manages sustained duration logic in Redis."""
    cache_key = f"auto_cond_state_{condition.id}"
    state = cache.get(cache_key)

    is_met = False

    # Type cast threshold
    try:
        threshold = float(condition.threshold) if isinstance(current_value, int | float) else condition.threshold
    except ValueError:
        threshold = condition.threshold

    # Evaluate
    if condition.operator == "gt":
        is_met = current_value > threshold
    elif condition.operator == "lt":
        is_met = current_value < threshold
    elif condition.operator == "gte":
        is_met = current_value >= threshold
    elif condition.operator == "lte":
        is_met = current_value <= threshold
    elif condition.operator == "eq":
        is_met = current_value == threshold
    elif condition.operator == "neq":
        is_met = current_value != threshold
    elif condition.operator == "is_true":
        is_met = bool(current_value) is True
    elif condition.operator == "is_false":
        is_met = bool(current_value) is False

    # Update state tracking for duration
    now_ts = timezone.now().timestamp()
    if is_met:
        if not state or not state.get("is_met"):
            # Just became met
            state = {"is_met": True, "met_since": now_ts}
            cache.set(cache_key, state, timeout=86400 * 7)  # Keep alive for a week
        return check_duration(condition, state["met_since"])
    else:
        if state and state.get("is_met"):
            # Condition broken
            cache.delete(cache_key)
        return False


def check_duration(condition, met_since_ts):
    if condition.duration_seconds <= 0:
        return True

    elapsed = timezone.now().timestamp() - met_since_ts
    return elapsed >= condition.duration_seconds


def execute_automation(automation, trigger_device=None):
    """Fires all actions associated with the automation."""
    automation.last_triggered_at = timezone.now()
    automation.save(update_fields=["last_triggered_at"])

    log_details = []
    has_error = False
    event_id = str(uuid4())

    for action in automation.actions.all():
        try:
            if action.action_type == "send_command":
                if action.target_device:
                    log_details.append(
                        "Command proposal created for "
                        f"{action.target_device.name}: {action.command_key}="
                        f"{action.command_payload.get('value', '')}. "
                        "Automations are read-only and cannot dispatch governed control."
                    )
            elif action.action_type == "webhook":
                if action.webhook_url:
                    response = requests.post(
                        action.webhook_url,
                        json={"automation": automation.name, "triggered_at": timezone.now().isoformat()},
                        headers=action.webhook_headers,
                        timeout=5,
                    )
                    response.raise_for_status()
                    log_details.append(f"Webhook fired: {action.webhook_url}")
            elif action.action_type == "notify_email":
                from .tasks import dispatch_automation_email_task

                dispatch_automation_email_task.delay(action.pk, event_id, trigger_device.pk if trigger_device else None)
                log_details.append(f"Email queued for {action.notify_emails}; delivery is tracked separately.")
            elif action.action_type == "create_ticket":
                from apps.maintenance.models import MaintenanceTicket

                t_device = action.target_device or trigger_device
                if t_device:
                    ticket = MaintenanceTicket.objects.create(
                        team=automation.team,
                        device=t_device,
                        title=f"[AUTO] {automation.name} on {t_device.name}",
                        description=(
                            f"Automated ticket generated by automation rule: {automation.name}.\n"
                            f"Device: {t_device.name}\n"
                            f"Description: {automation.description}"
                        ),
                        ticket_type=MaintenanceTicket.TypeChoices.REACTIVE,
                        priority=MaintenanceTicket.PriorityChoices.HIGH,
                        status=MaintenanceTicket.StatusChoices.OPEN,
                    )
                    log_details.append(f"Maintenance ticket TKT-{ticket.id} created for {t_device.name}")
                else:
                    log_details.append("Maintenance ticket creation failed: no device identified.")

        except Exception:
            logger.exception("Automation action failed for action %s", action.pk)
            log_details.append(
                "An automation step could not be completed. Check the rule settings and try again, or contact support."
            )
            has_error = True

    AutomationLog.objects.create(
        team=automation.team,
        automation=automation,
        status="partial" if has_error and len(log_details) > 1 else ("failed" if has_error else "success"),
        details="\n".join(log_details),
    )
    logger.info(f"Automation '{automation.name}' executed.")
