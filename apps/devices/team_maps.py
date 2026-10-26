"""Publish private, immutable snapshots of confirmed site signal maps."""

import uuid

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction

from .datapoint_maps import (
    datapoints_to_register_map,
    mapping_checksum,
    normalize_datapoints,
)
from .models import DeviceDatapointMap, DeviceTemplate


def reusable_signals(signals):
    points = normalize_datapoints(signals)
    if any(point["access"] != "read_only" for point in points):
        raise ValidationError("Remove write-capable signals before saving a reusable map.")
    for point in points:
        for field in ("normal_min", "normal_max", "safety_min", "safety_max"):
            point[field] = None
        point["alert_suggestion"] = ""
    return points


@transaction.atomic
def save_confirmed_team_map(
    *,
    device,
    team,
    name,
    manufacturer,
    model_number,
    firmware_scope="",
    program_scope="",
    source_url="",
    configuration_notes="",
):
    if device.team_id != team.pk or not device.template_id:
        raise ValidationError("Choose equipment belonging to this team.")
    name, manufacturer, model_number = (value.strip() for value in (name, manufacturer, model_number))
    if not all((name, manufacturer, model_number)):
        raise ValidationError("Map name, manufacturer, and exact model are required.")
    firmware_scope, program_scope, source_url = (value.strip() for value in (firmware_scope, program_scope, source_url))
    configuration_notes = configuration_notes.strip()[:2000]
    if device.device_type == "plc" and not program_scope:
        raise ValidationError("Identify the PLC program or register assignment before team reuse.")
    if source_url:
        URLValidator(schemes=["https"])(source_url)
    mapping = DeviceDatapointMap.objects.select_for_update().filter(device=device, team=team).first()
    if not mapping or mapping.status != DeviceDatapointMap.Status.CONFIRMED:
        raise ValidationError("Confirm live readings before saving a team map.")
    if mapping.confirmed_checksum != mapping_checksum(device, mapping.datapoints):
        raise ValidationError("The equipment settings changed. Test and confirm them again.")
    revision = mapping.revisions.filter(confirmed_checksum=mapping.confirmed_checksum).first()
    if not revision or revision.datapoints != mapping.datapoints:
        raise ValidationError("A confirmed map revision is required.")
    existing = DeviceTemplate.objects.filter(source_mapping_revision=revision).first()
    if existing:
        return existing
    points = reusable_signals(revision.datapoints)
    prior = device.template
    previous_snapshot = (
        DeviceTemplate.objects.filter(
            created_by_team=team,
            source_mapping_revision__mapping=mapping,
            catalog_key__isnull=False,
        )
        .order_by("-catalog_revision")
        .first()
    )
    family = previous_snapshot or (prior if prior.created_by_team_id == team.pk and prior.catalog_key else None)
    same_family = bool(family)
    if same_family:
        current = (
            DeviceTemplate.objects.select_for_update()
            .filter(created_by_team=team, catalog_key=family.catalog_key, is_current=True)
            .first()
        )
        key = family.catalog_key
        next_revision = (
            max(DeviceTemplate.objects.filter(catalog_key=key).values_list("catalog_revision", flat=True)) + 1
        )
    else:
        current = None
        key = f"team-{uuid.uuid4()}"
        next_revision = 1
    snapshot = DeviceTemplate.objects.create(
        name=name,
        manufacturer=manufacturer,
        model_number=model_number,
        device_type=device.device_type,
        protocol=device.protocol,
        mapping_strategy="site_defined",
        register_map=datapoints_to_register_map(points),
        default_polling_interval=prior.default_polling_interval,
        category=prior.category,
        source="user_created",
        source_url=source_url,
        created_by_team=team,
        catalog_key=key,
        catalog_revision=next_revision,
        source_mapping_revision=revision,
        firmware_scope=firmware_scope,
        program_scope=program_scope,
        evidence_status="legacy_unreviewed",
        is_verified=False,
        compatibility_notes=(
            (f"PLC program/register assignment: {program_scope}. " if program_scope else "")
            + "Confirm model, firmware, and readings on each device. "
            + configuration_notes
        ),
    )
    if current:
        current.is_current = False
        current.save(update_fields=["is_current"])
    return snapshot
