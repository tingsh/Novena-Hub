"""Customer equipment-map assistance and staff delivery workflow."""

import logging
import uuid
from datetime import timedelta

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.events.models import EmailDelivery
from apps.events.services import send_tracked_email

from .datapoint_maps import datapoints_to_register_map
from .models import DeviceTemplate, EquipmentTemplateRequest
from .team_maps import reusable_signals

logger = logging.getLogger("novena_hub")


class TemplateHelpRequestForm(forms.Form):
    manufacturer = forms.CharField(max_length=200, required=False)
    model_number = forms.CharField(max_length=200, required=False)
    protocol = forms.ChoiceField(
        choices=(("", "Not sure"), ("modbus_tcp", "Modbus TCP"), ("modbus_rtu", "Modbus RTU")),
        required=False,
    )
    device_type = forms.ChoiceField(
        choices=DeviceTemplate.DEVICE_TYPE_CHOICES,
        initial="other",
        required=False,
    )
    equipment_purpose = forms.CharField(max_length=2000)
    desired_signals = forms.CharField(max_length=2000)
    firmware_or_program = forms.CharField(max_length=200, required=False)
    documentation_url = forms.URLField(required=False)
    documentation_file = forms.FileField(required=False)
    nameplate_photo = forms.FileField(required=False)

    def clean_documentation_url(self):
        value = self.cleaned_data["documentation_url"]
        if value:
            URLValidator(schemes=["https"])(value)
        return value

    def clean_documentation_file(self):
        upload = self.cleaned_data["documentation_file"]
        if upload:
            if upload.size > 10 * 1024 * 1024 or not upload.name.lower().endswith(".pdf"):
                raise forms.ValidationError("Upload a PDF no larger than 10 MB.")
            if upload.read(5) != b"%PDF-":
                raise forms.ValidationError("The file is not a PDF.")
            upload.seek(0)
        return upload

    def clean_nameplate_photo(self):
        upload = self.cleaned_data["nameplate_photo"]
        if upload:
            if upload.size > 5 * 1024 * 1024 or not upload.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                raise forms.ValidationError("Upload a JPG, PNG, or WebP image no larger than 5 MB.")
            prefix = upload.read(12)
            upload.seek(0)
            is_image = (
                prefix.startswith(b"\xff\xd8\xff")
                or prefix.startswith(b"\x89PNG\r\n\x1a\n")
                or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP")
            )
            if not is_image:
                raise forms.ValidationError("The nameplate file is not a supported image.")
        return upload

    def clean(self):
        data = super().clean()
        if not data.get("model_number") and not data.get("nameplate_photo"):
            raise forms.ValidationError("Enter the exact model or attach a nameplate photo.")
        return data


def _send_help_email(*, help_request, to_customer=False):
    recipient = (
        help_request.requested_by.email
        if to_customer and help_request.requested_by
        else settings.PROJECT_METADATA.get("CONTACT_EMAIL", settings.DEFAULT_FROM_EMAIL)
    )
    if not recipient:
        return
    try:
        url = (
            reverse("web_team:devices:template_help_detail", args=[help_request.team.slug, help_request.pk])
            if to_customer
            else reverse("admin:devices_equipmenttemplaterequest_change", args=[help_request.pk])
        )
        absolute_url = settings.PROJECT_METADATA.get("URL", "").rstrip("/") + url
        reply_line = f"Customer reply: {help_request.customer_reply}\n" if not to_customer else ""
        send_tracked_email(
            team=help_request.team,
            notification_type=EmailDelivery.NotificationType.SUPPORT_REQUEST,
            subject=f"[Novena mapping help] {help_request.support_reference} · {help_request.get_status_display()}",
            text_body=(
                f"Equipment: {help_request.manufacturer} {help_request.model_number}\n"
                f"Status: {help_request.get_status_display()}\n"
                f"Reference: {help_request.support_reference}\n"
                f"Update: {help_request.customer_update}\n"
                + reply_line
                + f"Open the request in Novena Hub: {absolute_url}\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipients=[recipient],
            metadata={"equipment_template_request_id": help_request.pk},
        )
    except Exception:
        logger.exception("Could not send template help update for request %s", help_request.pk)


@transaction.atomic
def create_help_request(*, team, actor, form, run=None, setup_item=None, discovery_evidence=None):
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    if setup_item and (setup_item.team_id != team.pk or (run and setup_item.run_id != run.pk)):
        raise ValidationError("The selected equipment does not belong to this setup.")
    if setup_item:
        existing = (
            EquipmentTemplateRequest.objects.select_for_update()
            .filter(
                team=team,
                setup_item=setup_item,
            )
            .exclude(
                status__in=[
                    EquipmentTemplateRequest.Status.CLOSED,
                    EquipmentTemplateRequest.Status.COMPLETED,
                ]
            )
            .first()
        )
        if existing:
            return existing, False
    data = form.cleaned_data
    if not setup_item:
        duplicate = (
            EquipmentTemplateRequest.objects.filter(
                team=team,
                requested_by=actor,
                manufacturer=data["manufacturer"],
                model_number=data["model_number"],
                equipment_purpose=data["equipment_purpose"],
                desired_signals=data["desired_signals"],
                created_at__gte=timezone.now() - timedelta(minutes=5),
            )
            .exclude(
                status__in=[
                    EquipmentTemplateRequest.Status.CLOSED,
                    EquipmentTemplateRequest.Status.COMPLETED,
                ]
            )
            .first()
        )
        if duplicate:
            return duplicate, False
    help_request = EquipmentTemplateRequest.objects.create(
        team=team,
        run=run,
        setup_item=setup_item,
        requested_by=actor,
        manufacturer=data["manufacturer"],
        model_number=data["model_number"],
        protocol=data["protocol"],
        device_type=data["device_type"] or "other",
        equipment_purpose=data["equipment_purpose"],
        desired_signals=data["desired_signals"],
        firmware_or_program=data["firmware_or_program"],
        documentation_url=data["documentation_url"],
        documentation_file=data["documentation_file"],
        nameplate_photo=data["nameplate_photo"],
        discovery_evidence=discovery_evidence or {},
    )
    transaction.on_commit(lambda: _send_help_email(help_request=help_request))
    return help_request, True


@transaction.atomic
def deliver_help_map(*, help_request, actor):
    if not actor.is_staff:
        raise ValidationError("Only Novena staff can deliver a map.")
    help_request = EquipmentTemplateRequest.objects.select_for_update().get(pk=help_request.pk)
    if help_request.delivered_template_id:
        return help_request.delivered_template
    if help_request.status != EquipmentTemplateRequest.Status.IN_PROGRESS:
        raise ValidationError("Record accepted quote and payment before delivering a map.")
    if not help_request.quote_accepted_reference or not (
        help_request.payment_reference or help_request.payment_waived_reason
    ):
        raise ValidationError("Record accepted quote and payment or a waiver first.")
    if not all((help_request.manufacturer, help_request.model_number)) or help_request.protocol not in {
        "modbus_tcp",
        "modbus_rtu",
    }:
        raise ValidationError("Identify the exact manufacturer, model, and Modbus protocol first.")
    points = reusable_signals(help_request.proposed_signals)
    if help_request.device_type == "plc" and not help_request.firmware_or_program:
        raise ValidationError("Identify the PLC program/register assignment before delivery.")
    template = DeviceTemplate.objects.create(
        name=f"{help_request.manufacturer} {help_request.model_number} · Novena assisted",
        manufacturer=help_request.manufacturer,
        model_number=help_request.model_number,
        device_type=help_request.device_type,
        protocol=help_request.protocol,
        mapping_strategy="site_defined",
        register_map=datapoints_to_register_map(points),
        category="energy",
        source="novena_assisted",
        created_by_team=help_request.team,
        source_url=help_request.documentation_url,
        catalog_key=f"team-{uuid.uuid4()}",
        catalog_revision=1,
        evidence_status="legacy_unreviewed",
        is_verified=False,
        compatibility_notes=(
            "Private map prepared by Novena. Test and confirm readings on this equipment before deployment. "
            + help_request.map_configuration_notes.strip()
        ),
        firmware_scope=help_request.firmware_or_program if help_request.device_type != "plc" else "",
        program_scope=help_request.firmware_or_program if help_request.device_type == "plc" else "",
    )
    help_request.delivered_template = template
    help_request.status = EquipmentTemplateRequest.Status.READY_TO_TEST
    help_request.customer_update = (
        "Your private map is ready. Select it in Guided Setup and arrange a live reading check with Novena."
    )
    help_request.save(update_fields=["delivered_template", "status", "customer_update", "updated_at"])
    transaction.on_commit(lambda: _send_help_email(help_request=help_request, to_customer=True))
    return template
