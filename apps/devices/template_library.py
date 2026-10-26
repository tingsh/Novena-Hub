"""Customer-facing template preparation and catalog handoff."""

import json
import re
import uuid

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Case, CharField, Count, IntegerField, Q, Value, When
from django.db.models.functions import Cast, Lower, Replace
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.teams.decorators import require_permission
from apps.teams.roles import has_permission

from .datapoint_maps import datapoints_to_register_map, reset_device_datapoint_map_for_template
from .models import (
    DeploymentSetupItem,
    DeploymentSetupRun,
    DeviceTemplate,
    Gateway,
    PrivateTemplateDraft,
    SavedDeviceTemplate,
)
from .services import visible_templates_for_team
from .team_maps import reusable_signals


def template_origin(template):
    if template.created_by_team_id:
        return "Novena-assisted private" if template.source == "novena_assisted" else "Team private"
    return "Community contributed" if template.source == "community" else "Novena"


def template_evidence(template):
    if template.created_by_team_id:
        return "Live test required"
    return {
        "field_verified": "Field verified",
        "documented": "OEM documented",
        "retired": "Retired",
    }.get(template.evidence_status, "Review required")


def template_signals(template):
    return [
        {"key": key, **config, "display_scale": config.get("multiplier", config.get("scale", 1))}
        for key, config in (template.register_map or {}).items()
        if isinstance(config, dict)
    ]


def template_read_only(template):
    signals = template.register_map or {}
    return isinstance(signals, dict) and not any(
        isinstance(point, dict)
        and (point.get("writable") or point.get("writeFunctionCode") or point.get("functionCode") in {5, 6, 15, 16})
        for point in signals.values()
    )


def _searchable_queryset(team):
    return (
        visible_templates_for_team(team)
        .filter(protocol__in=["modbus_tcp", "modbus_rtu"])
        .filter(
            Q(created_by_team=team)
            | (
                Q(
                    created_by_team__isnull=True,
                    evidence_status__in=["documented", "field_verified"],
                    source_url__startswith="https://",
                    manufacturer__gt="",
                    model_number__gt="",
                )
                & ~Q(register_map={})
                & (Q(device_type="plc", program_scope__gt="") | ~Q(device_type="plc"))
            )
        )
    )


@require_permission("view_devices")
def library(request, team_slug):
    query = request.GET.get("q", "").strip()[:100]
    manufacturer = request.GET.get("manufacturer", "").strip()[:200]
    protocol = request.GET.get("protocol", "")
    equipment_type = request.GET.get("equipment_type", "")
    origin = request.GET.get("source", "")
    evidence = request.GET.get("evidence", "")
    base = _searchable_queryset(request.team)
    manufacturers = list(
        base.exclude(manufacturer="").values("manufacturer").annotate(count=Count("id")).order_by(Lower("manufacturer"))
    )
    templates = base
    if manufacturer:
        templates = templates.filter(manufacturer__iexact=manufacturer)
    if protocol in {"modbus_tcp", "modbus_rtu"}:
        templates = templates.filter(protocol=protocol)
    if equipment_type in dict(DeviceTemplate.DEVICE_TYPE_CHOICES):
        templates = templates.filter(device_type=equipment_type)
    if origin == "novena":
        templates = templates.filter(created_by_team__isnull=True, source="curated")
    elif origin == "community":
        templates = templates.filter(created_by_team__isnull=True, source="community")
    elif origin == "team":
        templates = templates.filter(created_by_team=request.team)
    if evidence in {"field_verified", "documented"}:
        templates = templates.filter(evidence_status=evidence)
    if query:
        compact = re.sub(r"[\s-]+", "", query.lower())
        templates = (
            templates.annotate(
                compact_model=Replace(Replace(Lower("model_number"), Value("-"), Value("")), Value(" "), Value("")),
                compact_name=Replace(Replace(Lower("name"), Value("-"), Value("")), Value(" "), Value("")),
                compact_aliases=Replace(
                    Replace(Lower(Cast("model_aliases", output_field=CharField())), Value("-"), Value("")),
                    Value(" "),
                    Value(""),
                ),
            )
            .filter(
                Q(name__icontains=query)
                | Q(manufacturer__icontains=query)
                | Q(model_number__icontains=query)
                | Q(model_aliases__icontains=query)
                | Q(compact_model__contains=compact)
                | Q(compact_name__contains=compact)
                | Q(compact_aliases__contains=compact)
            )
            .annotate(
                search_rank=Case(
                    When(compact_model=compact, then=Value(0)),
                    When(compact_aliases__contains=compact, then=Value(1)),
                    When(compact_model__startswith=compact, then=Value(2)),
                    default=Value(3),
                    output_field=IntegerField(),
                )
            )
            .order_by("search_rank", "manufacturer", "model_number", "-catalog_revision")
        )
    else:
        templates = templates.order_by("manufacturer", "model_number", "-catalog_revision")
    page = Paginator(templates, 24).get_page(request.GET.get("page"))
    saved_ids = set(SavedDeviceTemplate.objects.filter(team=request.team).values_list("template_id", flat=True))
    for template in page:
        template.library_origin = template_origin(template)
        template.library_evidence = template_evidence(template)
        template.signal_count = len(template_signals(template))
        template.saved_for_setup = template.pk in saved_ids
        template.library_read_only = template_read_only(template)
    selected = {
        "q": query,
        "manufacturer": manufacturer,
        "protocol": protocol,
        "equipment_type": equipment_type,
        "source": origin,
        "evidence": evidence,
    }
    saved = SavedDeviceTemplate.objects.filter(team=request.team).select_related("template").order_by("-created_at")[:6]
    saved = list(saved)
    for entry in saved:
        entry.selectable = entry.template.is_current and entry.template.evidence_status != "retired"
    drafts = PrivateTemplateDraft.objects.filter(team=request.team).order_by("-updated_at")[:6]
    return render(
        request,
        "devices/template_library.html",
        {
            "active_tab": "templates",
            "templates": page,
            "page_obj": page,
            "result_count": page.paginator.count,
            "manufacturers": manufacturers,
            "selected": selected,
            "saved_templates": saved,
            "private_drafts": drafts,
            "protocols": DeviceTemplate.PROTOCOL_CHOICES[:2],
            "equipment_types": DeviceTemplate.DEVICE_TYPE_CHOICES,
            "can_manage_templates": has_permission(request.user, request.team, "manage_devices"),
        },
    )


def _eligible_setup_items(team, template):
    editable = [
        DeploymentSetupItem.State.DISCOVERED,
        DeploymentSetupItem.State.TEMPLATE_SELECTED,
        DeploymentSetupItem.State.NEEDS_ATTENTION,
        DeploymentSetupItem.State.FAILED,
        DeploymentSetupItem.State.VALIDATED,
        DeploymentSetupItem.State.AWAITING_CONFIRMATION,
    ]
    active_runs = DeploymentSetupRun.objects.filter(
        team=team,
        state__in=[
            DeploymentSetupRun.State.DRAFT,
            DeploymentSetupRun.State.GATEWAY_CHECK,
            DeploymentSetupRun.State.DISCOVERING,
            DeploymentSetupRun.State.CONFIGURING,
        ],
    ).order_by("-created_at", "-pk")
    latest_run_ids = {}
    for run_id, gateway_id in active_runs.values_list("pk", "gateway_id"):
        latest_run_ids.setdefault(gateway_id, run_id)
    return (
        DeploymentSetupItem.objects.filter(
            team=team,
            run__team=team,
            run_id__in=latest_run_ids.values(),
            removed_at__isnull=True,
            state__in=editable,
        )
        .filter(
            Q(device__protocol=template.protocol, device__last_telemetry_at__isnull=True)
            | Q(device__isnull=True, candidate_data__connection=template.protocol)
            | Q(device__isnull=True, candidate_data__protocol=template.protocol)
        )
        .exclude(run__gateway__lifecycle_status__in=["released", "release_pending"])
        .select_related("device", "run", "run__gateway")
        .order_by("-run__created_at", "pk")
    )


def _known_identity_conflict(item, template):
    data = item.candidate_data or (item.device.discovery_meta if item.device else {}) or {}
    identity = data.get("identification") or {}
    known_model = identity.get("model") or data.get("model")
    if known_model:

        def compact(value):
            return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

        accepted = {compact(template.model_number), *(compact(alias) for alias in template.model_aliases or [])}
        if compact(known_model) not in accepted:
            return "The discovered model differs from this template. Check the equipment nameplate."
    known_program = identity.get("program") or data.get("program_signature")
    if (
        template.device_type == "plc"
        and known_program
        and template.program_scope
        and str(known_program).strip().lower() != template.program_scope.strip().lower()
    ):
        return "The discovered PLC program differs from this template."
    return ""


@require_permission("view_devices")
def detail(request, team_slug, pk):
    template = get_object_or_404(_searchable_queryset(request.team), pk=pk)
    template.library_origin = template_origin(template)
    template.library_evidence = template_evidence(template)
    template.library_read_only = template_read_only(template)
    saved = SavedDeviceTemplate.objects.filter(team=request.team, template=template).exists()
    gateways = Gateway.objects.filter(team=request.team).exclude(lifecycle_status__in=["released", "release_pending"])
    revisions = (
        DeviceTemplate.objects.filter(
            catalog_key=template.catalog_key, created_by_team=template.created_by_team
        ).order_by("-catalog_revision")
        if template.catalog_key
        else []
    )
    setup_items = list(_eligible_setup_items(request.team, template)[:50])
    for item in setup_items:
        item.library_equipment_name = (
            item.device.name
            if item.device
            else (item.candidate_data or {}).get("customer_name")
            or (item.candidate_data or {}).get("signature")
            or "Discovered equipment"
        )
    return render(
        request,
        "devices/template_detail.html",
        {
            "active_tab": "templates",
            "template": template,
            "signals": template_signals(template),
            "saved_for_setup": saved,
            "revisions": revisions,
            "has_gateway": gateways.exists(),
            "setup_items": setup_items,
            "can_manage_templates": has_permission(request.user, request.team, "manage_devices"),
        },
    )


@require_permission("manage_devices")
@require_POST
def save_for_setup(request, team_slug, pk):
    if request.POST.get("action") == "remove":
        saved = get_object_or_404(SavedDeviceTemplate, team=request.team, template_id=pk)
        saved.delete()
        messages.success(request, "Template removed from saved templates.")
        return redirect("web_team:devices:template_library", team_slug=team_slug)
    template = get_object_or_404(_searchable_queryset(request.team), pk=pk)
    SavedDeviceTemplate.objects.get_or_create(team=request.team, template=template, defaults={"saved_by": request.user})
    messages.success(request, "Template saved for setup. Live readings are tested after a Gateway is claimed.")
    return redirect("web_team:devices:template_detail", team_slug=team_slug, pk=pk)


@require_permission("manage_devices")
@require_POST
def use_in_setup(request, team_slug, pk):
    template = get_object_or_404(_searchable_queryset(request.team), pk=pk)
    if not template_read_only(template):
        messages.error(request, "This template contains write controls and cannot be reused in Guided Setup.")
        return redirect("web_team:devices:template_detail", team_slug=team_slug, pk=pk)
    with transaction.atomic():
        item = get_object_or_404(
            _eligible_setup_items(request.team, template).select_for_update(),
            pk=request.POST.get("item_id"),
        )
        if not request.POST.get("compatibility_confirmed"):
            messages.error(request, "Review the exact model, protocol, and configuration requirements first.")
            return redirect("web_team:devices:template_detail", team_slug=team_slug, pk=pk)
        if template.device_type == "plc" and (not template.program_scope or not request.POST.get("program_confirmed")):
            messages.error(request, "Confirm the exact PLC program and register assignment first.")
            return redirect("web_team:devices:template_detail", team_slug=team_slug, pk=pk)
        identity_conflict = _known_identity_conflict(item, template)
        if identity_conflict:
            messages.error(request, identity_conflict)
            return redirect("web_team:devices:template_detail", team_slug=team_slug, pk=pk)
        device = item.device
        if device:
            if device.team_id != request.team.pk or item.run.gateway_id != device.gateway_id:
                raise Http404
            device.template = template
            device.device_type = template.device_type
            metadata = dict(device.metadata or {})
            metadata["guided_setup_validation"] = "pending"
            device.metadata = metadata
            device.save(update_fields=["template", "device_type", "metadata", "updated_at"])
            if template.mapping_strategy == "site_defined":
                reset_device_datapoint_map_for_template(device=device, team=request.team)
        item.selected_template = template
        item.datapoints = []
        item.validation_result = {}
        item.validation_command = None
        item.trust_level = DeploymentSetupItem.Trust.UNVALIDATED
        item.state = DeploymentSetupItem.State.TEMPLATE_SELECTED
        item.save(
            update_fields=[
                "selected_template",
                "datapoints",
                "validation_result",
                "validation_command",
                "trust_level",
                "state",
                "updated_at",
            ]
        )
    request.session["onboarding_gateway_id"] = item.run.gateway_id
    if item.device_id:
        request.session["onboarding_device_id"] = item.device_id
    messages.success(request, "Template selected. Test its live readings in Guided Setup before confirmation.")
    return redirect("web_team:onboarding:step_3_discover", team_slug=team_slug)


def _draft_from_post(draft, data, actor):
    for field in (
        "name",
        "manufacturer",
        "model_number",
        "variant",
        "firmware_scope",
        "program_scope",
        "source_url",
        "configuration_notes",
    ):
        value = data.get(field, "").strip()
        max_length = draft._meta.get_field(field).max_length
        setattr(draft, field, value[:max_length] if max_length else value[:2000])
    for field, choices in (
        ("protocol", {"modbus_tcp", "modbus_rtu"}),
        ("device_type", dict(DeviceTemplate.DEVICE_TYPE_CHOICES)),
        ("category", dict(DeviceTemplate.VERTICAL_CHOICES)),
    ):
        value = data.get(field, "")
        if value not in choices:
            raise ValidationError(f"Choose a valid {field.replace('_', ' ')}.")
        setattr(draft, field, value)
    raw = data.get("signals_json", "[]")
    if len(raw) > 25000:
        raise ValidationError("The signal list is too large.")
    try:
        signals = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("The signal list could not be read.") from exc
    if not isinstance(signals, list) or len(signals) > 20 or any(not isinstance(row, dict) for row in signals):
        raise ValidationError("Use no more than 20 signal rows.")
    draft.signals = signals
    draft.created_by = draft.created_by or actor
    if draft.source_url:
        URLValidator(schemes=["https"])(draft.source_url)
    return draft


@require_permission("manage_devices")
def private_draft(request, team_slug, pk=None):
    draft = (
        get_object_or_404(PrivateTemplateDraft, pk=pk, team=request.team)
        if pk
        else PrivateTemplateDraft(team=request.team)
    )
    if request.method == "POST":
        try:
            _draft_from_post(draft, request.POST, request.user)
            draft.save()
            if request.POST.get("action") == "publish":
                template = publish_private_draft(draft)
                messages.success(
                    request, "Private template is ready in your team library. Test it on each device in Guided Setup."
                )
                return redirect("web_team:devices:template_detail", team_slug=team_slug, pk=template.pk)
            messages.success(request, "Private template draft saved. You can return before installing a Gateway.")
            return redirect("web_team:devices:template_draft_edit", team_slug=team_slug, pk=draft.pk)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return render(
        request,
        "devices/template_draft.html",
        {
            "active_tab": "templates",
            "draft": draft,
            "signals_json": json.dumps(draft.signals or []),
            "protocols": DeviceTemplate.PROTOCOL_CHOICES[:2],
            "equipment_types": DeviceTemplate.DEVICE_TYPE_CHOICES,
            "categories": DeviceTemplate.VERTICAL_CHOICES,
        },
    )


@transaction.atomic
def publish_private_draft(draft):
    if not all((draft.name, draft.manufacturer, draft.model_number)):
        raise ValidationError("Enter a template name, manufacturer, and exact model.")
    if draft.device_type == "plc" and not draft.program_scope:
        raise ValidationError("Identify the PLC program or register assignment before sharing with your team.")
    if draft.protocol not in {"modbus_tcp", "modbus_rtu"}:
        raise ValidationError("Choose Modbus TCP or Modbus RTU.")
    points = reusable_signals(draft.signals)
    if not draft.pk:
        draft.save()
    previous = draft.last_published_template
    if previous:
        previous = DeviceTemplate.objects.select_for_update().get(pk=previous.pk, created_by_team=draft.team)
        key = previous.catalog_key
        revision = previous.catalog_revision + 1
    else:
        key = f"team-{uuid.uuid4()}"
        revision = 1
    template = DeviceTemplate.objects.create(
        name=draft.name,
        manufacturer=draft.manufacturer,
        model_number=draft.model_number,
        variant=draft.variant,
        device_type=draft.device_type,
        protocol=draft.protocol,
        mapping_strategy="site_defined",
        register_map=datapoints_to_register_map(points),
        category=draft.category,
        source="user_created",
        source_url=draft.source_url,
        created_by_team=draft.team,
        catalog_key=key,
        catalog_revision=revision,
        firmware_scope=draft.firmware_scope,
        program_scope=draft.program_scope,
        compatibility_notes=draft.configuration_notes[:2000],
        evidence_status="legacy_unreviewed",
        is_verified=False,
    )
    if previous:
        previous.is_current = False
        previous.save(update_fields=["is_current"])
    draft.last_published_template = template
    draft.save(update_fields=["last_published_template", "updated_at"])
    return template
