import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from apps.alerts.models import Alert
from apps.maintenance.models import MaintenanceTicket, PreventiveSchedule
from apps.telemetry.models import TelemetryData

from .models import (
    ImpactBaseline,
    ImpactDataSource,
    ImpactMetricSnapshot,
    ImpactOpportunity,
    SiteImpactProfile,
)

METHODOLOGY_VERSION = "1.0"
MONEY_QUANTUM = Decimal("0.01")
VALUE_QUANTUM = Decimal("0.000001")
MIN_FINANCIAL_COVERAGE = Decimal("80")
NORMAL_CONFIDENCE_COVERAGE = Decimal("95")
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass
class MetricResult:
    key: str
    value: Decimal | None
    unit: str
    evidence: str
    coverage_pct: Decimal = Decimal("0")
    monetary_value: Decimal | None = None
    currency: str = ""
    source: ImpactDataSource | None = None
    warnings: list[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)
    confidence_override: str | None = None

    @property
    def confidence(self):
        if self.confidence_override:
            return self.confidence_override
        if self.coverage_pct < MIN_FINANCIAL_COVERAGE:
            return ImpactMetricSnapshot.Confidence.WITHHELD
        if self.coverage_pct < NORMAL_CONFIDENCE_COVERAGE:
            return ImpactMetricSnapshot.Confidence.LOW
        return ImpactMetricSnapshot.Confidence.NORMAL


def as_decimal(value, default=Decimal("0")):
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_value(value):
    return as_decimal(value).quantize(VALUE_QUANTUM, rounding=ROUND_HALF_UP)


def quantize_money(value):
    return as_decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def active_assumption(site_profile, at=None):
    at = at or timezone.now()
    return (
        site_profile.assumption_revisions.filter(effective_from__lte=at)
        .order_by("-effective_from", "-revision")
        .first()
    )


def site_period_for_date(site_profile, local_date):
    zone = ZoneInfo(site_profile.site.timezone)
    start = datetime.combine(local_date, time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def month_period(site_profile, year, month):
    zone = ZoneInfo(site_profile.site.timezone)
    start = datetime(year, month, 1, tzinfo=zone)
    end = datetime(year + 1, 1, 1, tzinfo=zone) if month == 12 else datetime(year, month + 1, 1, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def _parse_hhmm(value):
    hour, minute = str(value).split(":", 1)
    return time(int(hour), int(minute))


def is_operating_time(site_profile, timestamp):
    zone = ZoneInfo(site_profile.site.timezone)
    local = timestamp.astimezone(zone)
    override = (site_profile.schedule_overrides or {}).get(local.date().isoformat())
    windows = (
        override
        if override is not None
        else (site_profile.operating_schedule or {}).get(DAY_NAMES[local.weekday()], [])
    )
    local_clock = local.timetz().replace(tzinfo=None)
    for raw_start, raw_end in windows:
        start = _parse_hhmm(raw_start)
        end = _parse_hhmm(raw_end)
        if start <= end:
            if start <= local_clock < end:
                return True
        elif local_clock >= start or local_clock < end:
            return True
    return False


def _source_register(source):
    from apps.devices.datapoint_maps import effective_register_map

    return effective_register_map(source.device).get(source.telemetry_key, {})


def _source_expected_interval(source):
    register = _source_register(source)
    from apps.subscriptions.enforcement import get_effective_polling_interval_seconds

    value = max(
        float(register.get("expected_interval_seconds") or 0),
        get_effective_polling_interval_seconds(source.device),
    )
    try:
        return max(1, math.ceil(value))
    except (TypeError, ValueError):
        return 5


def _source_points(source, start, end):
    return list(
        TelemetryData.objects.filter(
            device=source.device,
            key=source.telemetry_key,
            timestamp__gte=start - timedelta(minutes=15),
            timestamp__lt=end,
            value_numeric__isnull=False,
        )
        .order_by("timestamp")
        .only("timestamp", "value_numeric")
    )


def _normalized_value(source, point):
    return as_decimal(point.value_numeric) * source.conversion_factor


def _is_plausible(source, value):
    bounds = _source_register(source).get("plausibility") or {}
    minimum = bounds.get("min")
    maximum = bounds.get("max")
    if minimum is not None and value < as_decimal(minimum):
        return False
    return maximum is None or value <= as_decimal(maximum)


def _counter_rollover_value(source):
    register = _source_register(source)
    explicit = register.get("counter_rollover_value")
    behavior = register.get("counter_behavior")
    if explicit is None and isinstance(behavior, dict) and behavior.get("type") == "rollover":
        explicit = behavior.get("maximum")
    if explicit is None:
        return None
    return as_decimal(explicit) * source.conversion_factor


def _segment_bounds(left, right, start, end):
    segment_start = max(left.timestamp, start)
    segment_end = min(right.timestamp, end)
    return segment_start, segment_end


def integrate_power_source(source, start, end):
    """Integrate instantaneous kW data using bounded trapezoids."""
    points = _source_points(source, start, end)
    expected_interval = _source_expected_interval(source)
    max_gap_seconds = min(max(expected_interval * 3, 60), 900)
    total_kwh = Decimal("0")
    after_hours_kwh = Decimal("0")
    covered_seconds = Decimal("0")
    after_hours_seconds = Decimal("0")
    bucket_energy = defaultdict(lambda: Decimal("0"))
    bucket_seconds = defaultdict(lambda: Decimal("0"))
    rejected_samples = 0

    for left, right in zip(points, points[1:], strict=False):
        segment_start, segment_end = _segment_bounds(left, right, start, end)
        seconds = Decimal(str((segment_end - segment_start).total_seconds()))
        raw_gap = (right.timestamp - left.timestamp).total_seconds()
        if seconds <= 0 or raw_gap <= 0 or raw_gap > max_gap_seconds:
            continue
        left_kw = _normalized_value(source, left)
        right_kw = _normalized_value(source, right)
        if not _is_plausible(source, left_kw) or not _is_plausible(source, right_kw):
            rejected_samples += 1
            continue
        average_kw = (left_kw + right_kw) / Decimal("2")
        energy = average_kw * seconds / Decimal("3600")
        total_kwh += energy
        covered_seconds += seconds
        midpoint = segment_start + (segment_end - segment_start) / 2
        if not is_operating_time(source.site_profile, midpoint):
            after_hours_kwh += energy
            after_hours_seconds += seconds
        bucket = int(midpoint.timestamp()) // 900
        bucket_energy[bucket] += energy
        bucket_seconds[bucket] += seconds

    peak_kw = Decimal("0")
    for bucket, energy in bucket_energy.items():
        seconds = bucket_seconds[bucket]
        if seconds:
            peak_kw = max(peak_kw, energy * Decimal("3600") / seconds)

    period_seconds = max(Decimal("1"), Decimal(str((end - start).total_seconds())))
    coverage = min(Decimal("100"), covered_seconds / period_seconds * Decimal("100"))
    return {
        "energy_kwh": total_kwh,
        "after_hours_kwh": after_hours_kwh,
        "after_hours_seconds": after_hours_seconds,
        "peak_kw": peak_kw,
        "coverage_pct": coverage,
        "sample_count": len(points),
        "rejected_samples": rejected_samples,
        "max_gap_seconds": max_gap_seconds,
    }


def integrate_counter_source(source, start, end):
    """Calculate positive cumulative-counter deltas while tolerating reset boundaries."""
    points = _source_points(source, start, end)
    expected_interval = _source_expected_interval(source)
    max_gap_seconds = min(max(expected_interval * 6, 120), 1800)
    total = Decimal("0")
    after_hours = Decimal("0")
    covered_seconds = Decimal("0")
    after_hours_seconds = Decimal("0")
    resets = 0
    rollovers = 0
    rejected_samples = 0
    rollover_value = _counter_rollover_value(source)

    for left, right in zip(points, points[1:], strict=False):
        segment_start, segment_end = _segment_bounds(left, right, start, end)
        seconds = Decimal(str((segment_end - segment_start).total_seconds()))
        raw_gap = (right.timestamp - left.timestamp).total_seconds()
        if seconds <= 0 or raw_gap <= 0 or raw_gap > max_gap_seconds:
            continue
        left_value = _normalized_value(source, left)
        right_value = _normalized_value(source, right)
        if not _is_plausible(source, left_value) or not _is_plausible(source, right_value):
            rejected_samples += 1
            continue
        delta = right_value - left_value
        if delta < 0:
            if (
                rollover_value is not None
                and left_value >= rollover_value * Decimal("0.8")
                and right_value <= rollover_value * Decimal("0.2")
            ):
                delta = rollover_value - left_value + right_value
                rollovers += 1
            else:
                resets += 1
                continue
        total += delta
        covered_seconds += seconds
        midpoint = segment_start + (segment_end - segment_start) / 2
        if not is_operating_time(source.site_profile, midpoint):
            after_hours += delta
            after_hours_seconds += seconds

    period_seconds = max(Decimal("1"), Decimal(str((end - start).total_seconds())))
    coverage = min(Decimal("100"), covered_seconds / period_seconds * Decimal("100"))
    return {
        "energy_kwh": total,
        "after_hours_kwh": after_hours,
        "after_hours_seconds": after_hours_seconds,
        "peak_kw": Decimal("0"),
        "coverage_pct": coverage,
        "sample_count": len(points),
        "counter_resets": resets,
        "counter_rollovers": rollovers,
        "rejected_samples": rejected_samples,
        "max_gap_seconds": max_gap_seconds,
    }


def _energy_sources(site_profile):
    confirmed = list(
        site_profile.data_sources.filter(
            verification_status=ImpactDataSource.VerificationStatus.CONFIRMED,
            include_in_totals=True,
            device__template__is_verified=True,
            device__template__evidence_status="field_verified",
            device__template__created_by_team__isnull=True,
            quantity_kind__in=[ImpactDataSource.QuantityKind.ENERGY, ImpactDataSource.QuantityKind.POWER],
        ).select_related("device", "device__template")
    )
    boundaries = [source for source in confirmed if source.source_role == ImpactDataSource.SourceRole.SITE_BOUNDARY]
    selected = boundaries or [
        source
        for source in confirmed
        if source.source_role in {ImpactDataSource.SourceRole.SUBMETER, ImpactDataSource.SourceRole.INDEPENDENT}
    ]

    by_device = defaultdict(list)
    for source in selected:
        by_device[source.device_id].append(source)
    preferred = []
    for sources in by_device.values():
        counters = [
            source
            for source in sources
            if source.quantity_kind == ImpactDataSource.QuantityKind.ENERGY
            and source.aggregation == ImpactDataSource.Aggregation.CUMULATIVE_COUNTER
        ]
        preferred.append(sorted(counters or sources, key=lambda item: item.priority)[0])
    return preferred, bool(boundaries)


def _data_quality_warning(source):
    warnings = []
    if source.quantity_kind == ImpactDataSource.QuantityKind.TEMPERATURE:
        if source.calibration_status == ImpactDataSource.CalibrationStatus.UNKNOWN:
            warnings.append("Calibration status is unknown.")
        elif source.calibration_status == ImpactDataSource.CalibrationStatus.EXPIRED or (
            source.calibration_expires_at and source.calibration_expires_at < timezone.localdate()
        ):
            warnings.append("Calibration has expired.")
    return warnings


def calculate_energy_metrics(site_profile, assumption, start, end):
    sources, has_boundary = _energy_sources(site_profile)
    if not sources:
        return [], ["Confirm a verified main meter or non-overlapping equipment meters."]

    total_energy = Decimal("0")
    after_hours = Decimal("0")
    after_hours_seconds = Decimal("0")
    peak_kw = Decimal("0")
    weighted_coverage = Decimal("0")
    results = []
    warnings = (
        []
        if has_boundary
        else ["No main site meter is confirmed. Totals use only confirmed meters that do not overlap."]
    )

    for source in sources:
        if source.aggregation == ImpactDataSource.Aggregation.CUMULATIVE_COUNTER:
            values = integrate_counter_source(source, start, end)
        else:
            values = integrate_power_source(source, start, end)
        total_energy += values["energy_kwh"]
        after_hours += values["after_hours_kwh"]
        after_hours_seconds += values["after_hours_seconds"]
        peak_kw = max(peak_kw, values["peak_kw"])
        weighted_coverage += values["coverage_pct"]
        source_warnings = []
        if values.get("counter_resets"):
            source_warnings.append(f"{values['counter_resets']} counter reset interval(s) were excluded.")
        if values.get("counter_rollovers"):
            source_warnings.append(f"{values['counter_rollovers']} counter rollover(s) were corrected.")
        if values.get("rejected_samples"):
            source_warnings.append(f"{values['rejected_samples']} implausible sample interval(s) were excluded.")
        warnings.extend(source_warnings)
        results.append(
            MetricResult(
                key="source_energy_kwh",
                value=quantize_value(values["energy_kwh"]),
                unit="kWh",
                evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
                coverage_pct=values["coverage_pct"],
                source=source,
                warnings=source_warnings,
                breakdown=values,
            )
        )

    coverage = weighted_coverage / Decimal(len(sources))
    tariff = assumption.tariff_per_kwh if assumption else None
    currency = assumption.currency if assumption else ""
    energy_cost = (
        quantize_money(total_energy * tariff) if tariff is not None and coverage >= MIN_FINANCIAL_COVERAGE else None
    )
    opportunity_kwh = None
    opportunity_value = None
    abnormal_excess_kwh = None
    baseline_source = "collecting"
    if assumption and assumption.expected_after_hours_base_kw is not None:
        expected = assumption.expected_after_hours_base_kw * after_hours_seconds / Decimal("3600")
        opportunity_kwh = max(Decimal("0"), after_hours - expected)
    baseline_energy, baseline_source = _latest_baseline_value(
        site_profile,
        "energy_consumption_kwh",
        start,
        end,
    )
    if baseline_energy is not None:
        abnormal_excess_kwh = max(Decimal("0"), total_energy - baseline_energy)
        opportunity_kwh = max(opportunity_kwh or Decimal("0"), abnormal_excess_kwh)
    if opportunity_kwh is not None and tariff is not None and coverage >= MIN_FINANCIAL_COVERAGE:
        opportunity_value = quantize_money(opportunity_kwh * tariff)

    results.extend(
        [
            MetricResult(
                key="energy_consumption_kwh",
                value=quantize_value(total_energy),
                unit="kWh",
                evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
                coverage_pct=coverage,
                monetary_value=energy_cost,
                currency=currency,
                warnings=warnings.copy(),
                breakdown={"source_count": len(sources), "has_boundary_meter": has_boundary},
            ),
            MetricResult(
                key="peak_demand_kw",
                value=quantize_value(peak_kw),
                unit="kW",
                evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
                coverage_pct=coverage,
            ),
            MetricResult(
                key="after_hours_energy_kwh",
                value=quantize_value(after_hours),
                unit="kWh",
                evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
                coverage_pct=coverage,
                monetary_value=quantize_money(after_hours * tariff)
                if tariff is not None and coverage >= MIN_FINANCIAL_COVERAGE
                else None,
                currency=currency,
                breakdown={"covered_after_hours": float(after_hours_seconds / Decimal("3600"))},
            ),
            MetricResult(
                key="abnormal_excess_energy_kwh",
                value=(quantize_value(abnormal_excess_kwh) if abnormal_excess_kwh is not None else None),
                unit="kWh",
                evidence=ImpactMetricSnapshot.EvidenceClass.ESTIMATED,
                coverage_pct=coverage,
                warnings=(
                    []
                    if abnormal_excess_kwh is not None
                    else ["A rolling or customer-supplied energy baseline is still being established."]
                ),
                breakdown={"baseline_source": baseline_source},
            ),
            MetricResult(
                key="estimated_energy_opportunity",
                value=quantize_value(opportunity_kwh) if opportunity_kwh is not None else None,
                unit="kWh",
                evidence=ImpactMetricSnapshot.EvidenceClass.ESTIMATED,
                coverage_pct=coverage,
                monetary_value=opportunity_value,
                currency=currency,
                warnings=[] if opportunity_kwh is not None else ["Configure the expected after-hours base load."],
                breakdown={
                    "tariff_per_kwh": str(tariff) if tariff is not None else None,
                    "expected_after_hours_base_kw": str(assumption.expected_after_hours_base_kw)
                    if assumption and assumption.expected_after_hours_base_kw is not None
                    else None,
                    "abnormal_excess_kwh": (
                        str(quantize_value(abnormal_excess_kwh)) if abnormal_excess_kwh is not None else None
                    ),
                    "de_duplication": "maximum_of_after_hours_and_total_abnormal_excess",
                    "baseline_source": baseline_source,
                },
            ),
        ]
    )
    return results, warnings


def calculate_power_factor_metric(site_profile, start, end):
    sources = site_profile.data_sources.filter(
        verification_status=ImpactDataSource.VerificationStatus.CONFIRMED,
        quantity_kind=ImpactDataSource.QuantityKind.POWER_FACTOR,
        device__template__is_verified=True,
        device__template__evidence_status="field_verified",
        device__template__created_by_team__isnull=True,
    ).select_related("device", "device__template")
    low_seconds = Decimal("0")
    covered_seconds = Decimal("0")
    for source in sources:
        points = _source_points(source, start, end)
        max_gap = min(max(_source_expected_interval(source) * 3, 60), 900)
        for left, right in zip(points, points[1:], strict=False):
            segment_start, segment_end = _segment_bounds(left, right, start, end)
            seconds = Decimal(str((segment_end - segment_start).total_seconds()))
            if seconds <= 0 or (right.timestamp - left.timestamp).total_seconds() > max_gap:
                continue
            covered_seconds += seconds
            if (_normalized_value(source, left) + _normalized_value(source, right)) / Decimal("2") < Decimal("0.9"):
                low_seconds += seconds
    period_seconds = max(Decimal("1"), Decimal(str((end - start).total_seconds())))
    coverage = min(Decimal("100"), covered_seconds / period_seconds * Decimal("100"))
    return MetricResult(
        key="low_power_factor_hours",
        value=quantize_value(low_seconds / Decimal("3600")),
        unit="h",
        evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
        coverage_pct=coverage,
    )


def calculate_runtime_metrics(site_profile, start, end):
    sources = list(
        site_profile.data_sources.filter(
            verification_status=ImpactDataSource.VerificationStatus.CONFIRMED,
            quantity_kind=ImpactDataSource.QuantityKind.RUNTIME,
            device__template__is_verified=True,
        ).select_related("device", "device__template")
    )
    if not sources:
        return []
    total_runtime = Decimal("0")
    after_hours_runtime = Decimal("0")
    covered_seconds = Decimal("0")
    for source in sources:
        if source.aggregation == ImpactDataSource.Aggregation.CUMULATIVE_COUNTER:
            values = integrate_counter_source(source, start, end)
            total_runtime += values["energy_kwh"]
            after_hours_runtime += values["after_hours_kwh"]
            covered_seconds += values["coverage_pct"] / Decimal("100") * Decimal(str((end - start).total_seconds()))
            continue
        points = _source_points(source, start, end)
        max_gap = min(max(_source_expected_interval(source) * 3, 60), 900)
        for left, right in zip(points, points[1:], strict=False):
            segment_start, segment_end = _segment_bounds(left, right, start, end)
            seconds = Decimal(str((segment_end - segment_start).total_seconds()))
            if seconds <= 0 or (right.timestamp - left.timestamp).total_seconds() > max_gap:
                continue
            value = (_normalized_value(source, left) + _normalized_value(source, right)) / Decimal("2")
            if not _is_plausible(source, value):
                continue
            covered_seconds += seconds
            if value > Decimal("0.5"):
                hours = seconds / Decimal("3600")
                total_runtime += hours
                midpoint = segment_start + (segment_end - segment_start) / 2
                if not is_operating_time(site_profile, midpoint):
                    after_hours_runtime += hours
    period_seconds = max(
        Decimal("1"),
        Decimal(str((end - start).total_seconds())) * Decimal(max(1, len(sources))),
    )
    coverage = min(Decimal("100"), covered_seconds / period_seconds * Decimal("100"))
    return [
        MetricResult(
            key="hvac_runtime_hours",
            value=quantize_value(total_runtime),
            unit="h",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=coverage,
        ),
        MetricResult(
            key="after_hours_hvac_runtime_hours",
            value=quantize_value(after_hours_runtime),
            unit="h",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=coverage,
        ),
    ]


def calculate_cold_chain_metrics(site_profile, assumption, start, end):
    sources = list(
        site_profile.data_sources.filter(
            verification_status=ImpactDataSource.VerificationStatus.CONFIRMED,
            quantity_kind=ImpactDataSource.QuantityKind.TEMPERATURE,
            device__template__is_verified=True,
        ).select_related("device", "device__template")
    )
    if not sources:
        return [], ["Confirm at least one verified temperature source."]
    if not assumption or assumption.cold_min_temperature is None or assumption.cold_max_temperature is None:
        return [], ["Configure the acceptable temperature range."]

    covered_seconds = Decimal("0")
    in_range_seconds = Decimal("0")
    excursion_count = 0
    unresolved_count = 0
    excursion_seconds = Decimal("0")
    max_deviation = Decimal("0")
    risk_avoided = Decimal("0")
    all_warnings = []
    excursion_delay_seconds = Decimal(assumption.excursion_delay_minutes * 60)
    critical_seconds = (
        Decimal(assumption.cold_critical_duration_minutes * 60) if assumption.cold_critical_duration_minutes else None
    )

    for source in sources:
        all_warnings.extend(_data_quality_warning(source))
        points = _source_points(source, start, end)
        max_gap = min(max(_source_expected_interval(source) * 3, 60), 900)
        active_excursion_seconds = Decimal("0")
        in_excursion = False
        for left, right in zip(points, points[1:], strict=False):
            segment_start, segment_end = _segment_bounds(left, right, start, end)
            seconds = Decimal(str((segment_end - segment_start).total_seconds()))
            if seconds <= 0 or (right.timestamp - left.timestamp).total_seconds() > max_gap:
                if in_excursion and active_excursion_seconds >= excursion_delay_seconds:
                    excursion_count += 1
                    unresolved_count += 1
                    excursion_seconds += active_excursion_seconds
                in_excursion = False
                active_excursion_seconds = Decimal("0")
                continue
            value = (_normalized_value(source, left) + _normalized_value(source, right)) / Decimal("2")
            if not _is_plausible(source, value):
                continue
            covered_seconds += seconds
            is_in_range = assumption.cold_min_temperature <= value <= assumption.cold_max_temperature
            if is_in_range:
                in_range_seconds += seconds
                if in_excursion and active_excursion_seconds >= excursion_delay_seconds:
                    excursion_count += 1
                    excursion_seconds += active_excursion_seconds
                    if (
                        critical_seconds
                        and assumption.cold_loss_per_critical_excursion is not None
                        and active_excursion_seconds < critical_seconds
                    ):
                        remaining_fraction = (critical_seconds - active_excursion_seconds) / critical_seconds
                        risk_avoided += assumption.cold_loss_per_critical_excursion * remaining_fraction
                in_excursion = False
                active_excursion_seconds = Decimal("0")
            else:
                if not in_excursion:
                    in_excursion = True
                active_excursion_seconds += seconds
                if value < assumption.cold_min_temperature:
                    max_deviation = max(max_deviation, assumption.cold_min_temperature - value)
                else:
                    max_deviation = max(max_deviation, value - assumption.cold_max_temperature)
        if in_excursion and active_excursion_seconds >= excursion_delay_seconds:
            excursion_count += 1
            unresolved_count += 1
            excursion_seconds += active_excursion_seconds

    period_seconds = max(Decimal("1"), Decimal(str((end - start).total_seconds())) * Decimal(len(sources)))
    coverage = min(Decimal("100"), covered_seconds / period_seconds * Decimal("100"))
    time_in_range = in_range_seconds / covered_seconds * Decimal("100") if covered_seconds else Decimal("0")
    currency = assumption.currency
    calibration_confidence = ImpactMetricSnapshot.Confidence.LOW if all_warnings else None
    return [
        MetricResult(
            key="time_in_range_pct",
            value=quantize_value(time_in_range),
            unit="%",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=coverage,
            warnings=list(dict.fromkeys(all_warnings)),
            confidence_override=calibration_confidence,
            breakdown={
                "target_min": str(assumption.cold_min_temperature),
                "target_max": str(assumption.cold_max_temperature),
                "source_count": len(sources),
            },
        ),
        MetricResult(
            key="temperature_excursions",
            value=Decimal(excursion_count),
            unit="events",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=coverage,
        ),
        MetricResult(
            key="temperature_excursion_duration_minutes",
            value=quantize_value(excursion_seconds / Decimal("60")),
            unit="min",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=coverage,
            breakdown={"minimum_excursion_minutes": assumption.excursion_delay_minutes},
        ),
        MetricResult(
            key="unresolved_excursions",
            value=Decimal(unresolved_count),
            unit="events",
            evidence=ImpactMetricSnapshot.EvidenceClass.MEASURED,
            coverage_pct=coverage,
        ),
        MetricResult(
            key="maximum_temperature_deviation",
            value=quantize_value(max_deviation),
            unit="°C",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=coverage,
        ),
        MetricResult(
            key="estimated_spoilage_risk_avoided",
            value=None,
            unit="",
            evidence=ImpactMetricSnapshot.EvidenceClass.ESTIMATED,
            coverage_pct=coverage,
            monetary_value=quantize_money(risk_avoided)
            if risk_avoided and coverage >= MIN_FINANCIAL_COVERAGE
            else None,
            currency=currency,
            warnings=list(dict.fromkeys(all_warnings))
            + (
                []
                if assumption.cold_loss_per_critical_excursion is not None and critical_seconds
                else ["Configure loss value and critical excursion duration to estimate risk avoided."]
            ),
            confidence_override=calibration_confidence,
            breakdown={
                "loss_per_critical_excursion": str(assumption.cold_loss_per_critical_excursion)
                if assumption.cold_loss_per_critical_excursion is not None
                else None,
                "critical_duration_minutes": assumption.cold_critical_duration_minutes,
            },
        ),
    ], list(dict.fromkeys(all_warnings))


def calculate_maintenance_metrics(site_profile, assumption, start, end):
    site = site_profile.site
    alerts = Alert.objects.filter(device__site=site, triggered_at__gte=start, triggered_at__lt=end)
    acknowledged = alerts.exclude(acknowledged_at=None)
    response_seconds = []
    for triggered_at, acknowledged_at in acknowledged.values_list("triggered_at", "acknowledged_at"):
        response_seconds.append(max(0, (acknowledged_at - triggered_at).total_seconds()))

    tickets = MaintenanceTicket.objects.filter(team=site_profile.team, device__site=site)
    period_tickets = tickets.filter(created_at__gte=start, created_at__lt=end)
    closed = period_tickets.filter(status__in=["resolved", "closed"])
    resolution_seconds = [
        max(0, (closed_at - created_at).total_seconds())
        for created_at, closed_at in closed.exclude(closed_at=None).values_list("created_at", "closed_at")
    ]
    downtime_minutes = period_tickets.aggregate(total=Sum("downtime_minutes"))["total"] or 0
    labor_minutes = period_tickets.aggregate(total=Sum("labor_minutes"))["total"] or 0
    open_tickets = tickets.filter(status__in=["open", "in_progress", "waiting"]).count()
    due_pm = PreventiveSchedule.objects.filter(
        team=site_profile.team,
        device__site=site,
        next_due_at__gte=start,
        next_due_at__lt=end,
    ).count()
    completed_pm = closed.filter(ticket_type=MaintenanceTicket.TypeChoices.PREVENTIVE).count()
    pm_rate = Decimal(completed_pm) / Decimal(due_pm) * Decimal("100") if due_pm else Decimal("100")
    average_response_minutes = (
        Decimal(str(sum(response_seconds) / len(response_seconds) / 60)) if response_seconds else None
    )
    average_resolution_minutes = (
        Decimal(str(sum(resolution_seconds) / len(resolution_seconds) / 60)) if resolution_seconds else None
    )
    recurring_faults = alerts.values("rule_id").annotate(event_count=Count("id")).filter(event_count__gte=2).count()

    currency = assumption.currency if assumption else ""
    downtime_cost = (
        quantize_money(Decimal(downtime_minutes) / Decimal("60") * assumption.downtime_cost_per_hour)
        if assumption and assumption.downtime_cost_per_hour is not None
        else None
    )
    labor_cost = (
        quantize_money(Decimal(labor_minutes) / Decimal("60") * assumption.labor_cost_per_hour)
        if assumption and assumption.labor_cost_per_hour is not None
        else None
    )
    return [
        MetricResult(
            key="alert_response_minutes",
            value=quantize_value(average_response_minutes) if average_response_minutes is not None else None,
            unit="min",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=Decimal("100"),
        ),
        MetricResult(
            key="maintenance_downtime_minutes",
            value=Decimal(downtime_minutes),
            unit="min",
            evidence=ImpactMetricSnapshot.EvidenceClass.MEASURED,
            coverage_pct=Decimal("100"),
            monetary_value=downtime_cost,
            currency=currency,
        ),
        MetricResult(
            key="ticket_resolution_minutes",
            value=(quantize_value(average_resolution_minutes) if average_resolution_minutes is not None else None),
            unit="min",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=Decimal("100"),
        ),
        MetricResult(
            key="recurring_faults",
            value=Decimal(recurring_faults),
            unit="rules",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=Decimal("100"),
            breakdown={"definition": "Alert rules triggered at least twice in the period"},
        ),
        MetricResult(
            key="maintenance_labor_minutes",
            value=Decimal(labor_minutes),
            unit="min",
            evidence=ImpactMetricSnapshot.EvidenceClass.MEASURED,
            coverage_pct=Decimal("100"),
            monetary_value=labor_cost,
            currency=currency,
        ),
        MetricResult(
            key="open_maintenance_work",
            value=Decimal(open_tickets),
            unit="tickets",
            evidence=ImpactMetricSnapshot.EvidenceClass.MEASURED,
            coverage_pct=Decimal("100"),
        ),
        MetricResult(
            key="preventive_maintenance_completion_pct",
            value=quantize_value(pm_rate),
            unit="%",
            evidence=ImpactMetricSnapshot.EvidenceClass.CALCULATED,
            coverage_pct=Decimal("100"),
            breakdown={"due": due_pm, "completed": completed_pm},
        ),
    ]


def _latest_baseline_value(site_profile, metric_key, start, end):
    period_days = Decimal(str((end - start).total_seconds())) / Decimal("86400")
    supplied = site_profile.supplied_baseline or {}
    if site_profile.baseline_strategy == SiteImpactProfile.BaselineStrategy.SUPPLIED:
        supplied_key = {
            "energy_consumption_kwh": "monthly_energy_kwh",
            "maintenance_downtime_minutes": "monthly_downtime_minutes",
            "maintenance_labor_minutes": "monthly_labor_minutes",
        }.get(metric_key)
        if supplied_key and supplied.get(supplied_key) is not None:
            monthly_value = as_decimal(supplied[supplied_key])
            return monthly_value * Decimal(str(period_days)) / Decimal("30"), "customer_supplied"

    baseline = (
        ImpactBaseline.objects.filter(
            site_profile=site_profile,
            source__isnull=True,
            metric_key=metric_key,
            status=ImpactBaseline.Status.READY,
            methodology_version=METHODOLOGY_VERSION,
        )
        .order_by("-revision")
        .first()
    )
    if not baseline or baseline.values.get("daily_average") is None:
        return None, "collecting"
    weekday_averages = baseline.values.get("weekday_daily_average") or {}
    if weekday_averages:
        zone = ZoneInfo(site_profile.site.timezone)
        local_start = start.astimezone(zone)
        local_end = end.astimezone(zone)
        cursor = local_start
        expected = Decimal("0")
        while cursor < local_end:
            next_midnight = datetime.combine(cursor.date() + timedelta(days=1), time.min, tzinfo=zone)
            segment_end = min(next_midnight, local_end)
            local_midnight = datetime.combine(cursor.date(), time.min, tzinfo=zone)
            day_seconds = Decimal(str((next_midnight - local_midnight).total_seconds()))
            segment_seconds = Decimal(str((segment_end - cursor).total_seconds()))
            daily_value = weekday_averages.get(str(cursor.weekday()), baseline.values["daily_average"])
            expected += as_decimal(daily_value) * segment_seconds / max(day_seconds, Decimal("1"))
            cursor = segment_end
        return expected, "rolling_28_day_comparable_weekdays_local_time"
    return (
        as_decimal(baseline.values["daily_average"]) * Decimal(str(period_days)),
        "rolling_28_day_comparable_local_days",
    )


def add_realized_value_metrics(site_profile, assumption, start, end, metrics):
    if not assumption:
        return metrics
    metric_map = {metric.key: metric for metric in metrics}
    realized = Decimal("0")
    breakdown = {}
    baseline_ready = False

    energy = metric_map.get("energy_consumption_kwh")
    if energy and energy.value is not None and assumption.tariff_per_kwh is not None:
        baseline, source = _latest_baseline_value(site_profile, energy.key, start, end)
        breakdown["energy_baseline_source"] = source
        if baseline is not None and energy.coverage_pct >= MIN_FINANCIAL_COVERAGE:
            baseline_ready = True
            saved_kwh = max(Decimal("0"), baseline - energy.value)
            energy_value = saved_kwh * assumption.tariff_per_kwh
            realized += energy_value
            breakdown["estimated_energy_reduction_kwh"] = str(quantize_value(saved_kwh))

    downtime = metric_map.get("maintenance_downtime_minutes")
    if downtime and downtime.value is not None and assumption.downtime_cost_per_hour is not None:
        baseline, source = _latest_baseline_value(site_profile, downtime.key, start, end)
        breakdown["downtime_baseline_source"] = source
        if baseline is not None:
            baseline_ready = True
            saved_minutes = max(Decimal("0"), baseline - downtime.value)
            realized += saved_minutes / Decimal("60") * assumption.downtime_cost_per_hour
            breakdown["estimated_downtime_reduction_minutes"] = str(quantize_value(saved_minutes))

    labor = metric_map.get("maintenance_labor_minutes")
    if labor and labor.value is not None and assumption.labor_cost_per_hour is not None:
        baseline, source = _latest_baseline_value(site_profile, labor.key, start, end)
        breakdown["labor_baseline_source"] = source
        if baseline is not None:
            baseline_ready = True
            saved_minutes = max(Decimal("0"), baseline - labor.value)
            realized += saved_minutes / Decimal("60") * assumption.labor_cost_per_hour
            breakdown["estimated_labor_reduction_minutes"] = str(quantize_value(saved_minutes))

    risk_reduction = metric_map.get("estimated_spoilage_risk_avoided")
    if risk_reduction and risk_reduction.monetary_value is not None:
        baseline_ready = True
        realized += risk_reduction.monetary_value
        breakdown["estimated_risk_reduction"] = str(risk_reduction.monetary_value)

    financial_coverage = min(
        [metric.coverage_pct for metric in metrics if metric.key in {"energy_consumption_kwh", "time_in_range_pct"}]
        or [Decimal("100")]
    )

    metrics.append(
        MetricResult(
            key="estimated_realized_value",
            value=None,
            unit="",
            evidence=ImpactMetricSnapshot.EvidenceClass.ESTIMATED,
            coverage_pct=financial_coverage,
            monetary_value=(
                quantize_money(realized) if baseline_ready and financial_coverage >= MIN_FINANCIAL_COVERAGE else None
            ),
            currency=assumption.currency,
            warnings=(
                [] if baseline_ready else ["A valid measured or customer-supplied baseline is still being established."]
            ),
            breakdown=breakdown,
        )
    )
    return metrics


def _period_type(start, end):
    seconds = (end - start).total_seconds()
    if seconds <= 3600:
        return ImpactMetricSnapshot.PeriodType.HOURLY
    if seconds <= 90000:
        return ImpactMetricSnapshot.PeriodType.DAILY
    return ImpactMetricSnapshot.PeriodType.MONTHLY


def _persist_result(site_profile, assumption, start, end, result, finalized):
    defaults = {
        "team": site_profile.team,
        "evidence_class": result.evidence,
        "value": result.value,
        "unit": result.unit,
        "monetary_value": result.monetary_value,
        "currency": result.currency,
        "coverage_pct": result.coverage_pct.quantize(Decimal("0.01")),
        "confidence": result.confidence,
        "assumption_revision": assumption,
        "breakdown": result.breakdown,
        "warnings": result.warnings,
        "finalized": finalized,
    }
    lookup = {
        "site_profile": site_profile,
        "source": result.source,
        "period_type": _period_type(start, end),
        "period_start": start,
        "period_end": end,
        "metric_key": result.key,
        "methodology_version": METHODOLOGY_VERSION,
        "revision": 1,
    }
    existing = ImpactMetricSnapshot.objects.filter(**lookup).first()
    if existing and existing.finalized and finalized:
        unchanged = all(getattr(existing, key) == value for key, value in defaults.items())
        if unchanged:
            return existing
        lookup["revision"] = (
            ImpactMetricSnapshot.objects.filter(
                site_profile=site_profile,
                source=result.source,
                period_type=lookup["period_type"],
                period_start=start,
                period_end=end,
                metric_key=result.key,
                methodology_version=METHODOLOGY_VERSION,
            ).aggregate(max_revision=Max("revision"))["max_revision"]
            or 0
        ) + 1
        return ImpactMetricSnapshot.objects.create(**lookup, **defaults)
    snapshot, _ = ImpactMetricSnapshot.objects.update_or_create(defaults=defaults, **lookup)
    return snapshot


@transaction.atomic
def calculate_site_period(site_profile, start, end, *, finalized=False):
    """Calculate and persist the profile-aware metrics for one site and period."""
    site_profile = SiteImpactProfile.objects.select_for_update().select_related("site", "team").get(pk=site_profile.pk)
    if not site_profile.enabled or not site_profile.team.is_active:
        return []
    assumption = active_assumption(site_profile, end)
    metrics = []
    warnings = []

    if site_profile.vertical_profile in {"factory_energy", "facilities_hvac"}:
        energy_metrics, energy_warnings = calculate_energy_metrics(site_profile, assumption, start, end)
        metrics.extend(energy_metrics)
        warnings.extend(energy_warnings)
        metrics.append(calculate_power_factor_metric(site_profile, start, end))
    if site_profile.vertical_profile == "facilities_hvac":
        metrics.extend(calculate_runtime_metrics(site_profile, start, end))
    if site_profile.vertical_profile == "cold_chain":
        cold_metrics, cold_warnings = calculate_cold_chain_metrics(site_profile, assumption, start, end)
        metrics.extend(cold_metrics)
        warnings.extend(cold_warnings)

    metrics.extend(calculate_maintenance_metrics(site_profile, assumption, start, end))
    if (end - start).total_seconds() >= 20 * 3600:
        metrics = add_realized_value_metrics(site_profile, assumption, start, end, metrics)

    snapshots = [_persist_result(site_profile, assumption, start, end, metric, finalized) for metric in metrics]
    financial_coverages = [
        metric.coverage_pct for metric in metrics if metric.key in {"energy_consumption_kwh", "time_in_range_pct"}
    ]
    coverage = min(financial_coverages or [Decimal("100")])
    if (
        not assumption
        or not site_profile.data_sources.filter(
            verification_status=ImpactDataSource.VerificationStatus.CONFIRMED
        ).exists()
    ):
        status = SiteImpactProfile.CalculationStatus.NEEDS_CONFIGURATION
    elif coverage < MIN_FINANCIAL_COVERAGE:
        status = SiteImpactProfile.CalculationStatus.LOW_COVERAGE
    elif any(metric.key == "estimated_realized_value" and metric.monetary_value is None for metric in metrics):
        status = SiteImpactProfile.CalculationStatus.COLLECTING_BASELINE
    else:
        status = SiteImpactProfile.CalculationStatus.READY
    site_profile.calculation_status = status
    site_profile.last_calculated_at = timezone.now()
    site_profile.last_error = ""
    site_profile.save(update_fields=["calculation_status", "last_calculated_at", "last_error", "updated_at"])

    sync_opportunities(site_profile, start, end, metrics)
    return snapshots


def sync_opportunities(site_profile, start, end, metrics):
    for metric in metrics:
        if metric.key not in {"estimated_energy_opportunity", "unresolved_excursions"}:
            continue
        quantity = metric.value or Decimal("0")
        has_value = quantity > 0 or (metric.monetary_value or Decimal("0")) > 0
        if not has_value:
            continue
        opportunity_type = (
            ImpactOpportunity.OpportunityType.AFTER_HOURS
            if metric.key == "estimated_energy_opportunity"
            else ImpactOpportunity.OpportunityType.TEMPERATURE_EXCURSION
        )
        raw_fingerprint = f"{site_profile.pk}:{opportunity_type}:{start.date().isoformat()}"
        fingerprint = hashlib.sha256(raw_fingerprint.encode()).hexdigest()
        defaults = {
            "site_profile": site_profile,
            "source": metric.source,
            "opportunity_type": opportunity_type,
            "title": "After-hours energy opportunity"
            if opportunity_type == ImpactOpportunity.OpportunityType.AFTER_HOURS
            else "Temperature excursion needs attention",
            "description": "Review the measured condition, assumptions, and corrective work.",
            "quantity": metric.value,
            "unit": metric.unit,
            "estimated_value": metric.monetary_value,
            "currency": metric.currency,
            "detected_at": start,
        }
        opportunity, _ = ImpactOpportunity.objects.update_or_create(
            team=site_profile.team,
            fingerprint=fingerprint,
            defaults=defaults,
        )
        related_alerts = list(
            Alert.objects.filter(
                device__site=site_profile.site,
                triggered_at__gte=start,
                triggered_at__lt=end,
            )
        )
        opportunity.alerts.add(*related_alerts)
        alert_ids = [str(alert.id) for alert in related_alerts]
        related_tickets = MaintenanceTicket.objects.filter(
            team=site_profile.team,
            device__site=site_profile.site,
        ).filter(Q(created_at__gte=start, created_at__lt=end) | Q(alert_reference__in=alert_ids))
        opportunity.tickets.add(*related_tickets)
