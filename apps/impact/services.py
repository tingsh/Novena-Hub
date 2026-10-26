from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.subscriptions.helpers import subscription_is_active

from .calculations import (
    METHODOLOGY_VERSION,
    MetricResult,
    _persist_result,
    as_decimal,
    month_period,
    quantize_money,
    quantize_value,
)
from .models import (
    BusinessImpactProfile,
    ImpactAssumptionRevision,
    ImpactBaseline,
    ImpactDataSource,
    ImpactMetricSnapshot,
    ImpactOpportunity,
    SiteImpactProfile,
)

KEY_SEMANTICS = {
    "active_power": (ImpactDataSource.QuantityKind.POWER, ImpactDataSource.Aggregation.INSTANTANEOUS, "kW"),
    "power": (ImpactDataSource.QuantityKind.POWER, ImpactDataSource.Aggregation.INSTANTANEOUS, "kW"),
    "energy": (
        ImpactDataSource.QuantityKind.ENERGY,
        ImpactDataSource.Aggregation.CUMULATIVE_COUNTER,
        "kWh",
    ),
    "total_energy": (
        ImpactDataSource.QuantityKind.ENERGY,
        ImpactDataSource.Aggregation.CUMULATIVE_COUNTER,
        "kWh",
    ),
    "power_factor": (
        ImpactDataSource.QuantityKind.POWER_FACTOR,
        ImpactDataSource.Aggregation.INSTANTANEOUS,
        "",
    ),
    "temperature": (
        ImpactDataSource.QuantityKind.TEMPERATURE,
        ImpactDataSource.Aggregation.INSTANTANEOUS,
        "°C",
    ),
    "run_hours": (
        ImpactDataSource.QuantityKind.RUNTIME,
        ImpactDataSource.Aggregation.CUMULATIVE_COUNTER,
        "h",
    ),
}

UNIT_CONVERSIONS = {
    ("W", "kW"): Decimal("0.001"),
    ("kW", "kW"): Decimal("1"),
    ("Wh", "kWh"): Decimal("0.001"),
    ("kWh", "kWh"): Decimal("1"),
    ("C", "°C"): Decimal("1"),
    ("degC", "°C"): Decimal("1"),
    ("°C", "°C"): Decimal("1"),
    ("h", "h"): Decimal("1"),
    ("", ""): Decimal("1"),
}
SUPPORTED_IMPACT_PROFILES = {"factory_energy", "cold_chain", "facilities_hvac"}

SUM_METRICS = {
    "energy_consumption_kwh",
    "after_hours_energy_kwh",
    "abnormal_excess_energy_kwh",
    "estimated_energy_opportunity",
    "temperature_excursions",
    "maintenance_downtime_minutes",
    "maintenance_labor_minutes",
    "estimated_realized_value",
}
MAX_METRICS = {"peak_demand_kw", "maximum_temperature_deviation", "unresolved_excursions", "open_maintenance_work"}
METRIC_LABELS = {
    "energy_consumption_kwh": "Energy consumption",
    "peak_demand_kw": "15-minute peak demand",
    "after_hours_energy_kwh": "After-hours energy",
    "abnormal_excess_energy_kwh": "Abnormal excess energy",
    "estimated_energy_opportunity": "Estimated energy opportunity",
    "low_power_factor_duration_minutes": "Low power-factor duration",
    "time_in_range_pct": "Time in configured range",
    "temperature_excursions": "Temperature excursions",
    "temperature_excursion_duration_minutes": "Excursion duration",
    "unresolved_excursions": "Unresolved excursions",
    "maximum_temperature_deviation": "Maximum temperature deviation",
    "estimated_spoilage_risk_avoided": "Estimated spoilage risk avoided",
    "hvac_after_hours_runtime_hours": "After-hours HVAC runtime",
    "alert_response_minutes": "Alert response time",
    "ticket_resolution_minutes": "Ticket resolution time",
    "recurring_faults": "Recurring faults",
    "maintenance_downtime_minutes": "Recorded downtime",
    "maintenance_labor_minutes": "Recorded maintenance labor",
    "open_maintenance_work": "Maintenance backlog",
    "preventive_maintenance_completion_pct": "Preventive maintenance completion",
    "estimated_realized_value": "Estimated realized value",
}


@dataclass
class ImpactSummary:
    version: str
    site_id: int
    site_name: str
    vertical_profile: str
    period_start: str
    period_end: str
    currency: str
    estimated_opportunity: Decimal | None
    estimated_realized_value: Decimal | None
    investment_cost: Decimal | None
    net_benefit: Decimal | None
    roi_pct: Decimal | None
    payback_months: Decimal | None
    coverage_pct: Decimal
    confidence: str
    status: str
    metrics: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    leading_opportunities: list[dict] = field(default_factory=list)
    top_contributors: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def ensure_business_profile(team):
    profile, _ = BusinessImpactProfile.objects.get_or_create(
        team=team,
        defaults={"currency": "SGD", "roi_start_date": timezone.localdate()},
    )
    return profile


def ensure_site_profile(site):
    profile, _ = SiteImpactProfile.objects.get_or_create(
        team=site.team,
        site=site,
        defaults={
            "vertical_profile": site.solution_profile,
            "enabled": site.solution_profile in SUPPORTED_IMPACT_PROFILES,
        },
    )
    if profile.vertical_profile != site.solution_profile and not profile.snapshots.exists():
        profile.vertical_profile = site.solution_profile
        profile.enabled = site.solution_profile in SUPPORTED_IMPACT_PROFILES
        profile.save(update_fields=["vertical_profile", "enabled", "updated_at"])
    ensure_business_profile(site.team)
    if not profile.assumption_revisions.exists():
        ImpactAssumptionRevision.objects.create(
            team=site.team,
            site_profile=profile,
            revision=1,
            currency=ensure_business_profile(site.team).currency,
        )
    return profile


def create_assumption_revision(site_profile, values, *, user=None, change_note=""):
    latest = site_profile.assumption_revisions.order_by("-revision").first()
    revision = (latest.revision if latest else 0) + 1
    field_names = {
        field.name
        for field in ImpactAssumptionRevision._meta.fields
        if field.name
        not in {
            "id",
            "team",
            "site_profile",
            "revision",
            "changed_by",
            "change_note",
            "effective_from",
            "created_at",
            "updated_at",
        }
    }
    data = {}
    for field_name in field_names:
        if field_name in values:
            data[field_name] = values[field_name]
        elif latest:
            data[field_name] = getattr(latest, field_name)
    assumption = ImpactAssumptionRevision(
        team=site_profile.team,
        site_profile=site_profile,
        revision=revision,
        changed_by=user,
        change_note=change_note,
        effective_from=values.get("effective_from") or timezone.now(),
        **data,
    )
    assumption.full_clean()
    assumption.save()
    return assumption


def infer_datapoint_semantics(key, config):
    explicit_quantity = config.get("quantity_kind")
    explicit_aggregation = config.get("aggregation")
    explicit_unit = config.get("canonical_unit")
    if explicit_quantity and explicit_aggregation:
        canonical_unit = explicit_unit if explicit_unit is not None else config.get("unit", "")
        factor = as_decimal(config.get("conversion_factor"), Decimal("1"))
        return explicit_quantity, explicit_aggregation, canonical_unit, factor

    normalized_key = str(key).lower()
    match = KEY_SEMANTICS.get(normalized_key)
    if not match:
        if "temperature" in normalized_key or normalized_key.endswith("_temp"):
            match = KEY_SEMANTICS["temperature"]
        elif "power_factor" in normalized_key:
            match = KEY_SEMANTICS["power_factor"]
        elif "energy" in normalized_key or "yield" in normalized_key:
            match = KEY_SEMANTICS["energy"]
        elif "power" in normalized_key:
            match = KEY_SEMANTICS["active_power"]
    if not match:
        return None
    quantity, aggregation, canonical_unit = match
    raw_unit = config.get("unit", canonical_unit)
    factor = UNIT_CONVERSIONS.get((raw_unit, canonical_unit))
    if factor is None:
        return None
    return quantity, aggregation, canonical_unit, factor


def semantic_datapoint_metadata(key, config, expected_interval=5):
    semantics = infer_datapoint_semantics(key, config)
    if not semantics:
        return {}
    quantity, aggregation, canonical_unit, factor = semantics
    return {
        "quantity_kind": quantity,
        "aggregation": aggregation,
        "canonical_unit": canonical_unit,
        "conversion_factor": str(factor),
        "counter_behavior": config.get("counter_behavior", "resettable")
        if aggregation == ImpactDataSource.Aggregation.CUMULATIVE_COUNTER
        else "",
        "expected_interval_seconds": int(config.get("expected_interval_seconds") or expected_interval or 5),
        "plausibility": config.get("plausibility", {}),
        "impact_roles": config.get("impact_roles", []),
    }


@transaction.atomic
def suggest_data_sources(site):
    profile = ensure_site_profile(site)
    suggestions = []
    boundary_exists = profile.data_sources.filter(source_role=ImpactDataSource.SourceRole.SITE_BOUNDARY).exists()
    for device in site.devices.select_related("template").filter(template__is_verified=True):
        if device.template.category == "energy" and (
            device.template.evidence_status != "field_verified" or device.template.created_by_team_id
        ):
            continue
        from apps.devices.datapoint_maps import effective_register_map

        register_map = effective_register_map(device)
        for key, config in register_map.items():
            if not isinstance(config, dict):
                continue
            semantics = infer_datapoint_semantics(key, config)
            if not semantics:
                continue
            quantity, aggregation, canonical_unit, factor = semantics
            if quantity not in {
                ImpactDataSource.QuantityKind.POWER,
                ImpactDataSource.QuantityKind.ENERGY,
                ImpactDataSource.QuantityKind.POWER_FACTOR,
                ImpactDataSource.QuantityKind.TEMPERATURE,
                ImpactDataSource.QuantityKind.RUNTIME,
            }:
                continue
            if quantity in {ImpactDataSource.QuantityKind.POWER, ImpactDataSource.QuantityKind.ENERGY}:
                if device.energy_category == "utility" and not boundary_exists:
                    role = ImpactDataSource.SourceRole.SITE_BOUNDARY
                    boundary_exists = True
                else:
                    role = ImpactDataSource.SourceRole.INDEPENDENT
            else:
                role = ImpactDataSource.SourceRole.MONITORING_ONLY
            calibration = (
                ImpactDataSource.CalibrationStatus.UNKNOWN
                if quantity == ImpactDataSource.QuantityKind.TEMPERATURE
                else ImpactDataSource.CalibrationStatus.NOT_APPLICABLE
            )
            source, _ = ImpactDataSource.objects.get_or_create(
                team=site.team,
                site_profile=profile,
                device=device,
                telemetry_key=key,
                defaults={
                    "quantity_kind": quantity,
                    "aggregation": aggregation,
                    "canonical_unit": canonical_unit,
                    "conversion_factor": factor,
                    "source_role": role,
                    "include_in_totals": False,
                    "calibration_status": calibration,
                },
            )
            suggestions.append(source)
    return suggestions


@transaction.atomic
def confirm_data_source(
    source,
    *,
    source_role,
    include_in_totals,
    calibration_status=None,
    calibration_accuracy=None,
    calibration_expires_at=None,
):
    include_in_totals = include_in_totals and source_role != ImpactDataSource.SourceRole.MONITORING_ONLY
    if include_in_totals and source_role == ImpactDataSource.SourceRole.SITE_BOUNDARY:
        source.site_profile.data_sources.exclude(pk=source.pk).filter(
            quantity_kind__in=[
                ImpactDataSource.QuantityKind.POWER,
                ImpactDataSource.QuantityKind.ENERGY,
            ]
        ).update(include_in_totals=False)
    source.source_role = source_role
    source.include_in_totals = include_in_totals
    source.verification_status = ImpactDataSource.VerificationStatus.CONFIRMED
    if calibration_status:
        source.calibration_status = calibration_status
    source.calibration_accuracy = calibration_accuracy
    source.calibration_expires_at = calibration_expires_at
    source.full_clean()
    source.save()
    return source


def subscription_monthly_cost(team, currency):
    subscription = team.active_stripe_subscription
    if not subscription or not subscription_is_active(subscription):
        return None
    total = Decimal("0")
    for item in subscription.items.select_related("price"):
        price = item.price
        if (price.currency or "").upper() != currency.upper() or price.unit_amount_decimal is None:
            return None
        amount = as_decimal(price.unit_amount_decimal) / Decimal("100")
        recurring = price.recurring or {}
        interval = recurring.get("interval")
        count = as_decimal(recurring.get("interval_count"), Decimal("1"))
        if interval == "year":
            amount = amount * count / Decimal("12")
        elif interval == "month":
            amount = amount / count
        else:
            return None
        quantity = as_decimal(getattr(item, "quantity", 1) or 1)
        total += amount * quantity
    return quantize_money(total)


def monthly_recurring_investment_cost(profile):
    recurring = profile.recurring_monthly_cost
    if profile.use_subscription_cost:
        subscription_cost = subscription_monthly_cost(profile.team, profile.currency)
        if subscription_cost is not None:
            recurring += subscription_cost
    return quantize_money(recurring)


def monthly_investment_cost(profile):
    recurring = monthly_recurring_investment_cost(profile)
    amortized = profile.one_time_investment / Decimal(max(1, profile.amortization_months))
    return quantize_money(recurring + amortized)


def _latest_period_snapshots(site_profile, start, end):
    rows = (
        ImpactMetricSnapshot.objects.filter(
            site_profile=site_profile,
            source__isnull=True,
            period_type=ImpactMetricSnapshot.PeriodType.MONTHLY,
            period_start=start,
            period_end=end,
        )
        .order_by("metric_key", "-revision")
        .select_related("assumption_revision")
    )
    latest = {}
    for row in rows:
        latest.setdefault(row.metric_key, row)
    return latest


@transaction.atomic
def refresh_site_baselines(site_profile):
    """Version rolling baselines from four weeks of complete, good-coverage local days."""
    scopes = [
        ("energy_consumption_kwh", None),
        ("maintenance_downtime_minutes", None),
        ("maintenance_labor_minutes", None),
    ]
    scopes.extend(
        ("source_energy_kwh", source)
        for source in site_profile.data_sources.filter(
            verification_status=ImpactDataSource.VerificationStatus.CONFIRMED,
            include_in_totals=True,
        )
    )
    baselines = []
    for metric_key, source in scopes:
        rows = ImpactMetricSnapshot.objects.filter(
            site_profile=site_profile,
            source=source,
            period_type=ImpactMetricSnapshot.PeriodType.DAILY,
            metric_key=metric_key,
            finalized=True,
            value__isnull=False,
            coverage_pct__gte=80,
        ).order_by("-period_start", "-revision")[:56]
        latest_by_day = {}
        for row in rows:
            local_day = row.period_start.astimezone(ZoneInfo(site_profile.site.timezone)).date()
            latest_by_day.setdefault(local_day, row)
            if len(latest_by_day) == 28:
                break
        comparable = list(latest_by_day.values())
        if len(comparable) < 14:
            continue
        values = [row.value for row in comparable]
        average = sum(values, Decimal("0")) / Decimal(len(values))
        coverage = sum((row.coverage_pct for row in comparable), Decimal("0")) / Decimal(len(comparable))
        weekday_values = defaultdict(list)
        for row in comparable:
            local_day = row.period_start.astimezone(ZoneInfo(site_profile.site.timezone)).date()
            weekday_values[str(local_day.weekday())].append(row.value)
        payload = {
            "daily_average": str(quantize_value(average)),
            "weekday_daily_average": {
                weekday: str(quantize_value(sum(day_values, Decimal("0")) / Decimal(len(day_values))))
                for weekday, day_values in weekday_values.items()
            },
            "comparable_days": len(comparable),
            "local_timezone": site_profile.site.timezone,
            "window_days": 28,
        }
        previous = (
            site_profile.baselines.filter(
                source=source,
                metric_key=metric_key,
                methodology_version=METHODOLOGY_VERSION,
            )
            .order_by("-revision")
            .first()
        )
        if previous and previous.values == payload and previous.status == ImpactBaseline.Status.READY:
            baselines.append(previous)
            continue
        baseline = ImpactBaseline.objects.create(
            team=site_profile.team,
            site_profile=site_profile,
            source=source,
            metric_key=metric_key,
            period_start=min(row.period_start for row in comparable),
            period_end=max(row.period_end for row in comparable),
            strategy=SiteImpactProfile.BaselineStrategy.ROLLING,
            status=ImpactBaseline.Status.READY,
            values=payload,
            coverage_pct=coverage,
            methodology_version=METHODOLOGY_VERSION,
            revision=(previous.revision if previous else 0) + 1,
        )
        baselines.append(baseline)
    return baselines


def aggregate_daily_snapshots(site_profile, start, end, *, finalized=False):
    daily = (
        ImpactMetricSnapshot.objects.filter(
            site_profile=site_profile,
            period_type=ImpactMetricSnapshot.PeriodType.DAILY,
            period_start__gte=start,
            period_start__lt=end,
        )
        .order_by("period_start", "metric_key", "-revision")
        .select_related("assumption_revision")
    )
    latest = {}
    for snapshot in daily:
        latest.setdefault((snapshot.period_start, snapshot.metric_key, snapshot.source_id), snapshot)
    grouped = {}
    for snapshot in latest.values():
        grouped.setdefault((snapshot.metric_key, snapshot.source_id), []).append(snapshot)

    output = []
    for (metric_key, _source_id), snapshots in grouped.items():
        coverage = sum((row.coverage_pct for row in snapshots), Decimal("0")) / Decimal(len(snapshots))
        values = [row.value for row in snapshots if row.value is not None]
        monetary_values = [row.monetary_value for row in snapshots if row.monetary_value is not None]
        if metric_key in SUM_METRICS:
            value = sum(values, Decimal("0")) if values else None
            money = sum(monetary_values, Decimal("0")) if monetary_values else None
        elif metric_key in MAX_METRICS:
            value = max(values) if values else None
            money = max(monetary_values) if monetary_values else None
        else:
            value = sum(values, Decimal("0")) / Decimal(len(values)) if values else None
            money = sum(monetary_values, Decimal("0")) / Decimal(len(monetary_values)) if monetary_values else None
        first = snapshots[0]
        confidences = {row.confidence for row in snapshots}
        confidence_override = (
            ImpactMetricSnapshot.Confidence.WITHHELD
            if ImpactMetricSnapshot.Confidence.WITHHELD in confidences
            else ImpactMetricSnapshot.Confidence.LOW
            if ImpactMetricSnapshot.Confidence.LOW in confidences
            else None
        )
        result = MetricResult(
            key=metric_key,
            value=quantize_value(value) if value is not None else None,
            unit=first.unit,
            evidence=first.evidence_class,
            coverage_pct=coverage,
            monetary_value=quantize_money(money) if money is not None else None,
            currency=first.currency,
            source=first.source,
            confidence_override=confidence_override,
            warnings=list(dict.fromkeys(warning for row in snapshots for warning in row.warnings)),
            breakdown={
                "daily_snapshot_count": len(snapshots),
                "aggregation": "sum" if metric_key in SUM_METRICS else "rollup",
                "assumption_revisions": sorted(
                    {row.assumption_revision.revision for row in snapshots if row.assumption_revision_id}
                ),
            },
        )
        output.append(_persist_result(site_profile, first.assumption_revision, start, end, result, finalized))
    return output


def build_site_impact_summary(site_profile, year=None, month=None):
    now = timezone.now().astimezone(ZoneInfo(site_profile.site.timezone))
    year = year or now.year
    month = month or now.month
    start, end = month_period(site_profile, year, month)
    snapshots = _latest_period_snapshots(site_profile, start, end)
    business_profile = ensure_business_profile(site_profile.team)
    enabled_site_count = max(
        1,
        SiteImpactProfile.objects.filter(
            team=site_profile.team,
            enabled=True,
            include_in_reports=True,
        ).count(),
    )
    investment = quantize_money(monthly_investment_cost(business_profile) / Decimal(enabled_site_count))
    realized = snapshots.get("estimated_realized_value")
    opportunity = snapshots.get("estimated_energy_opportunity")
    realized_value = realized.monetary_value if realized else None
    opportunity_value = opportunity.monetary_value if opportunity else None
    calculation_currency = (
        (realized.currency if realized and realized.currency else "")
        or (opportunity.currency if opportunity and opportunity.currency else "")
        or business_profile.currency
    )
    currency_conflict = calculation_currency != business_profile.currency
    if currency_conflict:
        realized_value = None
        opportunity_value = None
    net_benefit = quantize_money(realized_value - investment) if realized_value is not None else None
    roi = (
        (net_benefit / investment * Decimal("100")).quantize(Decimal("0.1"))
        if net_benefit is not None and investment > 0
        else None
    )
    recurring_share = monthly_recurring_investment_cost(business_profile) / Decimal(enabled_site_count)
    one_time_share = business_profile.one_time_investment / Decimal(enabled_site_count)
    positive_run_rate = realized_value - recurring_share if realized_value is not None else Decimal("0")
    payback = (
        (one_time_share / positive_run_rate).quantize(Decimal("0.1"))
        if positive_run_rate > 0 and one_time_share > 0
        else None
    )
    key_snapshots = [
        snapshot
        for key, snapshot in snapshots.items()
        if snapshot.source_id is None and key not in {"source_energy_kwh"}
    ]
    coverage_values = [
        snapshot.coverage_pct
        for snapshot in key_snapshots
        if snapshot.metric_key in {"energy_consumption_kwh", "time_in_range_pct"}
    ]
    coverage = min(coverage_values or [Decimal("0")])
    warnings = list(dict.fromkeys(warning for snapshot in key_snapshots for warning in snapshot.warnings))
    if currency_conflict:
        warnings.append("Financial totals are withheld because the calculation and team currencies differ.")
    leading = site_profile.opportunities.exclude(status=ImpactOpportunity.Status.RESOLVED).order_by(
        "-estimated_value", "-detected_at"
    )[:5]
    source_rows = (
        ImpactMetricSnapshot.objects.filter(
            site_profile=site_profile,
            source__isnull=False,
            period_type=ImpactMetricSnapshot.PeriodType.MONTHLY,
            period_start=start,
            period_end=end,
            metric_key="source_energy_kwh",
        )
        .select_related("source", "source__device")
        .order_by("source_id", "-revision")
    )
    latest_source_rows = {}
    for row in source_rows:
        latest_source_rows.setdefault(row.source_id, row)
    period_days = Decimal(str((end - start).total_seconds())) / Decimal("86400")
    top_contributors = []
    for row in latest_source_rows.values():
        baseline = (
            site_profile.baselines.filter(
                source_id=row.source_id,
                metric_key="source_energy_kwh",
                status=ImpactBaseline.Status.READY,
            )
            .order_by("-revision")
            .first()
        )
        expected = (
            as_decimal(baseline.values["daily_average"]) * period_days
            if baseline and baseline.values.get("daily_average") is not None
            else None
        )
        excess = max(Decimal("0"), row.value - expected) if row.value is not None and expected is not None else None
        top_contributors.append(
            {
                "source_id": row.source_id,
                "device_id": row.source.device_id,
                "device_name": row.source.device.name,
                "value": row.value,
                "unit": row.unit,
                "abnormal_excess": quantize_value(excess) if excess is not None else None,
                "coverage_pct": row.coverage_pct,
            }
        )
    top_contributors.sort(
        key=lambda item: item["value"] or Decimal("0"),
        reverse=True,
    )
    return ImpactSummary(
        version="1",
        site_id=site_profile.site_id,
        site_name=site_profile.site.name,
        vertical_profile=site_profile.vertical_profile,
        period_start=start.date().isoformat(),
        period_end=(end.date()).isoformat(),
        currency=calculation_currency,
        estimated_opportunity=opportunity_value,
        estimated_realized_value=realized_value,
        investment_cost=investment,
        net_benefit=net_benefit,
        roi_pct=roi,
        payback_months=payback,
        coverage_pct=coverage,
        confidence=realized.confidence if realized else ImpactMetricSnapshot.Confidence.WITHHELD,
        status=site_profile.calculation_status,
        metrics=[
            {
                "key": snapshot.metric_key,
                "label": METRIC_LABELS.get(snapshot.metric_key, snapshot.metric_key.replace("_", " ").title()),
                "value": snapshot.value,
                "unit": snapshot.unit,
                "monetary_value": snapshot.monetary_value,
                "currency": snapshot.currency,
                "evidence": snapshot.evidence_class,
                "coverage_pct": snapshot.coverage_pct,
                "confidence": snapshot.confidence,
                "warnings": snapshot.warnings,
                "breakdown": snapshot.breakdown,
            }
            for snapshot in key_snapshots
        ],
        warnings=warnings,
        leading_opportunities=[
            {
                "id": opportunity.id,
                "title": opportunity.title,
                "status": opportunity.status,
                "estimated_value": opportunity.estimated_value,
                "currency": opportunity.currency,
            }
            for opportunity in leading
        ],
        top_contributors=top_contributors[:10],
    )


def build_impact_readiness(site_profile, summary):
    latest = site_profile.assumption_revisions.order_by("-revision").first()
    if site_profile.vertical_profile == "cold_chain":
        assumptions_ready = bool(
            latest and latest.cold_min_temperature is not None and latest.cold_max_temperature is not None
        )
        assumption_detail = "Configured temperature range"
    else:
        assumptions_ready = bool(
            latest
            and any(
                value is not None
                for value in (
                    latest.tariff_per_kwh,
                    latest.downtime_cost_per_hour,
                    latest.labor_cost_per_hour,
                )
            )
        )
        assumption_detail = "Tariff or operational cost assumptions"
    source_ready = site_profile.data_sources.filter(
        verification_status=ImpactDataSource.VerificationStatus.CONFIRMED,
    ).exists()
    supplied_ready = site_profile.baseline_strategy == SiteImpactProfile.BaselineStrategy.SUPPLIED and bool(
        site_profile.supplied_baseline
    )
    baseline_ready = supplied_ready or site_profile.baselines.filter(status=ImpactBaseline.Status.READY).exists()
    business = ensure_business_profile(site_profile.team)
    investment_ready = monthly_investment_cost(business) > 0
    return [
        {
            "label": "Meter or sensor confirmed",
            "ready": source_ready,
            "detail": "Your team has confirmed which reading should be used",
        },
        {"label": "Business inputs entered", "ready": assumptions_ready, "detail": assumption_detail},
        {
            "label": "Usable operating data",
            "ready": summary.coverage_pct >= Decimal("80"),
            "detail": f"{summary.coverage_pct}% of the period has usable readings",
        },
        {
            "label": "Comparison period",
            "ready": baseline_ready,
            "detail": "Monthly figures from your team, or at least 14 comparable measured days",
        },
        {
            "label": "Implementation cost",
            "ready": investment_ready,
            "detail": "Monthly cost, or the one-time cost spread across its useful period",
        },
    ]


def build_team_impact_summary(team, year=None, month=None, *, site_ids=None):
    profile_rows = SiteImpactProfile.objects.filter(team=team, enabled=True, include_in_reports=True)
    if site_ids is not None:
        profile_rows = profile_rows.filter(site_id__in=site_ids)
    profiles = list(profile_rows.select_related("site"))
    summaries = [build_site_impact_summary(profile, year, month) for profile in profiles]
    business = ensure_business_profile(team)
    compatible = [summary for summary in summaries if summary.currency == business.currency]
    realized_values = [
        summary.estimated_realized_value for summary in compatible if summary.estimated_realized_value is not None
    ]
    opportunity_values = [
        summary.estimated_opportunity for summary in compatible if summary.estimated_opportunity is not None
    ]
    realized = sum(realized_values, Decimal("0")) if realized_values else None
    opportunity = sum(opportunity_values, Decimal("0")) if opportunity_values else None
    total_enabled_sites = max(
        1,
        SiteImpactProfile.objects.filter(
            team=team,
            enabled=True,
            include_in_reports=True,
        ).count(),
    )
    scope_share = Decimal(len(profiles)) / Decimal(total_enabled_sites)
    investment = quantize_money(monthly_investment_cost(business) * scope_share)
    net = quantize_money(realized - investment) if realized is not None else None
    roi = (net / investment * Decimal("100")).quantize(Decimal("0.1")) if net is not None and investment > 0 else None
    recurring_scope = monthly_recurring_investment_cost(business) * scope_share
    one_time_scope = business.one_time_investment * scope_share
    positive_run_rate = realized - recurring_scope if realized is not None else Decimal("0")
    payback = (
        (one_time_scope / positive_run_rate).quantize(Decimal("0.1"))
        if positive_run_rate > 0 and one_time_scope > 0
        else None
    )
    coverage = min([summary.coverage_pct for summary in summaries] or [Decimal("0")])
    leading_candidates = [
        {
            **opportunity,
            "site_id": summary.site_id,
            "site_name": summary.site_name,
        }
        for summary in summaries
        for opportunity in summary.leading_opportunities
    ]
    leading_candidates.sort(
        key=lambda item: (
            item["estimated_value"] is not None,
            item["estimated_value"] or Decimal("0"),
        ),
        reverse=True,
    )
    return {
        "currency": business.currency,
        "estimated_opportunity": opportunity,
        "estimated_realized_value": realized,
        "investment_cost": investment,
        "net_benefit": net,
        "roi_pct": roi,
        "payback_months": payback,
        "coverage_pct": coverage,
        "mixed_currency": len(compatible) != len(summaries),
        "confidence": (
            ImpactMetricSnapshot.Confidence.NORMAL
            if coverage >= 95
            else ImpactMetricSnapshot.Confidence.LOW
            if coverage >= 80
            else ImpactMetricSnapshot.Confidence.WITHHELD
        ),
        "leading_opportunity": leading_candidates[0] if leading_candidates else None,
        "sites": [summary.to_dict() for summary in summaries],
    }
