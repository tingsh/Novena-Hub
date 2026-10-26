"""Consent, review and publication of customer-supplied signal maps."""

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from .datapoint_maps import datapoints_to_register_map, mapping_checksum
from .models import DeviceDatapointMap, DeviceTemplate, TemplateShareSubmission


@transaction.atomic
def submit_mapping_for_review(*, device, team, actor, consent, source_document_url=""):
    """Capture the confirmed revision only; never send connection or telemetry data."""
    if not consent:
        raise ValidationError("Choose the sharing checkbox before submitting this map.")
    source_document_url = source_document_url.strip()
    if source_document_url:
        URLValidator(schemes=["https"])(source_document_url)
    if device.team_id != team.pk or not device.template_id:
        raise ValidationError("This equipment is not available to your team.")
    template = device.template
    if template.created_by_team_id != team.pk or template.source == "curated":
        raise ValidationError("Only your team's private templates can be shared.")
    mapping = DeviceDatapointMap.objects.select_for_update().filter(device=device, team=team).first()
    if not mapping or mapping.status != DeviceDatapointMap.Status.CONFIRMED:
        raise ValidationError("Confirm live readings before sharing this map.")
    if not mapping.confirmed_checksum or mapping.confirmed_checksum != mapping_checksum(device, mapping.datapoints):
        raise ValidationError("The mapping has changed since confirmation. Test and confirm it again.")
    revision = mapping.revisions.filter(confirmed_checksum=mapping.confirmed_checksum).first()
    if not revision or revision.datapoints != mapping.datapoints:
        raise ValidationError("A confirmed mapping revision is required before sharing.")
    if TemplateShareSubmission.objects.filter(revision=revision).exists():
        raise ValidationError("This revision has already been submitted to Novena.")
    register_map = datapoints_to_register_map(revision.datapoints)
    if not register_map:
        raise ValidationError("Add at least one signal before sharing.")
    return TemplateShareSubmission.objects.create(
        team=team,
        template=template,
        revision=revision,
        register_map_snapshot=register_map,
        manufacturer=template.manufacturer,
        model_number=template.model_number,
        protocol=template.protocol,
        device_type=template.device_type,
        category=template.category,
        source_document_url=source_document_url or template.source_url,
        submitted_by=actor,
    )


@transaction.atomic
def publish_reviewed_submission(*, submission, reviewer, evidence_status):
    """Publish a separate anonymous global revision after an internal evidence review."""
    if not reviewer.is_staff:
        raise ValidationError("Only Novena staff may publish a reviewed template.")
    if evidence_status not in {"documented", "field_verified"}:
        raise ValidationError("Choose OEM documented or field verified evidence.")
    submission = TemplateShareSubmission.objects.select_for_update().get(pk=submission.pk)
    if submission.status == TemplateShareSubmission.Status.PUBLISHED:
        if evidence_status == "field_verified" and submission.published_template.evidence_status == "documented":
            if not submission.field_verification_evidence.strip():
                raise ValidationError("Record representative hardware evidence before field verification.")
            previous = DeviceTemplate.objects.select_for_update().get(pk=submission.published_template_id)
            promoted = DeviceTemplate.objects.create(
                name=previous.name, manufacturer=previous.manufacturer, model_number=previous.model_number,
                model_aliases=previous.model_aliases, variant=previous.variant,
                device_type=previous.device_type, protocol=previous.protocol,
                mapping_strategy=previous.mapping_strategy, register_map=previous.register_map,
                discovery_hints=previous.discovery_hints,
                default_polling_interval=previous.default_polling_interval,
                category=previous.category, alert_presets=[], source=previous.source,
                source_url=previous.source_url, firmware_scope=previous.firmware_scope,
                program_scope=previous.program_scope, compatibility_notes=previous.compatibility_notes,
                catalog_key=previous.catalog_key, catalog_revision=previous.catalog_revision + 1,
                evidence_status="field_verified", is_verified=True,
            )
            previous.is_current = False
            previous.save(update_fields=["is_current"])
            submission.published_template = promoted
            submission.reviewed_by = reviewer
            submission.reviewed_at = timezone.now()
            submission.save(update_fields=["published_template", "reviewed_by", "reviewed_at"])
            return promoted
        return submission.published_template
    if submission.status not in {
        TemplateShareSubmission.Status.SUBMITTED,
        TemplateShareSubmission.Status.REVIEWING,
    }:
        raise ValidationError("This submission is not awaiting review.")
    if (
        submission.revision.team_id != submission.team_id
        or submission.register_map_snapshot != datapoints_to_register_map(submission.revision.datapoints)
    ):
        raise ValidationError("The submitted revision no longer matches its review snapshot.")
    required = {
        "manufacturer": submission.manufacturer,
        "exact model": submission.model_number,
        "OEM register document URL": submission.source_document_url,
        "review notes": submission.review_notes,
        "public compatibility notes": submission.public_compatibility_notes,
        "cross-equipment applicability evidence": submission.portability_evidence,
    }
    if evidence_status == "field_verified":
        required["hardware verification evidence"] = submission.field_verification_evidence
    missing = [label for label, value in required.items() if not str(value or "").strip()]
    if missing:
        raise ValidationError("Complete the review first: " + ", ".join(missing) + ".")
    URLValidator(schemes=["https"])(submission.source_document_url)
    if any(
        point.get("writable") or point.get("writeFunctionCode")
        for point in submission.register_map_snapshot.values()
    ):
        raise ValidationError("Remove write-capable signals before publishing a read-only catalog map.")
    current = DeviceTemplate.objects.select_for_update().filter(
        source__in=["curated", "community"],
        is_current=True,
        manufacturer__iexact=submission.manufacturer,
        model_number__iexact=submission.model_number,
        protocol=submission.protocol,
        variant__iexact=submission.template.variant,
    ).first()
    # A site's alert and safety bands are not portable OEM signal definitions.
    register_map = {
        key: {
            field: value
            for field, value in point.items()
            if field not in {"normal_min", "normal_max", "safety_min", "safety_max", "alert_suggestion"}
        }
        for key, point in submission.register_map_snapshot.items()
    }
    published = DeviceTemplate.objects.create(
        name=f"{submission.manufacturer} {submission.model_number}",
        manufacturer=submission.manufacturer,
        model_number=submission.model_number,
        device_type=submission.device_type,
        protocol=submission.protocol,
        mapping_strategy="site_defined" if submission.device_type == "plc" else "fixed",
        register_map=register_map,
        category=submission.category,
        default_polling_interval=submission.template.default_polling_interval,
        source="community",
        source_url=submission.source_document_url,
        catalog_key=current.catalog_key if current and current.catalog_key else f"customer-reviewed-{submission.pk}",
        catalog_revision=(current.catalog_revision or 0) + 1 if current and current.catalog_key else 1,
        evidence_status=evidence_status,
        is_verified=evidence_status == "field_verified",
        compatibility_notes=submission.public_compatibility_notes,
        variant=submission.template.variant,
        program_scope=(submission.template.program_scope or submission.public_compatibility_notes)[:200]
        if submission.device_type == "plc" else "",
        firmware_scope=submission.template.firmware_scope,
        alert_presets=[],
    )
    if current:
        current.is_current = False
        current.save(update_fields=["is_current"])
    submission.status = TemplateShareSubmission.Status.PUBLISHED
    submission.published_template = published
    submission.reviewed_by = reviewer
    submission.reviewed_at = timezone.now()
    submission.save(update_fields=["status", "published_template", "reviewed_by", "reviewed_at"])
    return published


def publish_verified_submission(*, submission, reviewer):
    """Keep the established staff action for representative hardware evidence."""
    return publish_reviewed_submission(
        submission=submission, reviewer=reviewer, evidence_status="field_verified"
    )
