from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.impact.calculations import (
    _energy_sources,
    calculate_cold_chain_metrics,
    integrate_counter_source,
    integrate_power_source,
    is_operating_time,
)
from apps.impact.models import ImpactDataSource, ImpactMetricSnapshot
from apps.telemetry.models import TelemetryData

from .base import ImpactTestCase


class ScheduleAndEnergyCalculationTests(ImpactTestCase):
    def test_documented_energy_template_cannot_feed_impact_even_if_legacy_flag_is_true(self):
        template = self.create_template({"energy_import": {"unit": "kWh"}})
        template.evidence_status = "documented"
        template.save(update_fields=["evidence_status"])
        device = self.create_device(template, energy_category="utility")
        self.create_source(device, "energy_import")

        sources, has_boundary = _energy_sources(self.profile)

        self.assertEqual(sources, [])
        self.assertFalse(has_boundary)

    def test_schedule_handles_midnight_weekends_and_date_override(self):
        self.profile.operating_schedule = {
            "monday": [["22:00", "06:00"]],
            "tuesday": [],
            "wednesday": [],
            "thursday": [],
            "friday": [],
            "saturday": [],
            "sunday": [],
        }
        self.profile.schedule_overrides = {"2026-07-28": [["09:00", "10:00"]]}
        self.profile.save()

        self.assertTrue(
            is_operating_time(
                self.profile,
                datetime(2026, 7, 27, 15, 0, tzinfo=UTC),
            )
        )
        self.assertTrue(
            is_operating_time(
                self.profile,
                datetime(2026, 7, 28, 1, 30, tzinfo=UTC),
            )
        )
        self.assertFalse(
            is_operating_time(
                self.profile,
                datetime(2026, 7, 28, 2, 30, tzinfo=UTC),
            )
        )

    def test_boundary_meter_prevents_submeter_double_counting(self):
        template = self.create_template({"energy": {"unit": "kWh", "expected_interval_seconds": 300}})
        main = self.create_device(template, name="Main", energy_category="utility")
        sub = self.create_device(template, name="Submeter")
        main_source = self.create_source(main, "energy")
        self.create_source(
            sub,
            "energy",
            role=ImpactDataSource.SourceRole.SUBMETER,
        )

        sources, has_boundary = _energy_sources(self.profile)

        self.assertTrue(has_boundary)
        self.assertEqual(sources, [main_source])

    def test_counter_rollover_reset_duplicates_and_out_of_order_are_bounded(self):
        template = self.create_template(
            {
                "energy": {
                    "unit": "kWh",
                    "expected_interval_seconds": 300,
                    "counter_rollover_value": 999,
                    "plausibility": {"min": 0, "max": 999},
                }
            }
        )
        device = self.create_device(template)
        source = self.create_source(device, "energy")
        start = datetime(2026, 7, 1, tzinfo=UTC)
        samples = [
            (start, 990),
            (start + timedelta(minutes=5), 5),
            (start + timedelta(minutes=5), 5),
            (start + timedelta(minutes=10), 2),
            (start + timedelta(minutes=15), 7),
        ]
        for timestamp, value in reversed(samples):
            TelemetryData.objects.create(
                device=device,
                timestamp=timestamp,
                key="energy",
                value_numeric=value,
            )

        result = integrate_counter_source(
            source,
            start,
            start + timedelta(minutes=16),
        )

        self.assertEqual(result["counter_rollovers"], 1)
        self.assertEqual(result["counter_resets"], 1)
        self.assertEqual(result["energy_kwh"], Decimal("19"))

    def test_power_integration_rejects_implausible_samples_and_reports_coverage(self):
        template = self.create_template(
            {
                "active_power": {
                    "unit": "kW",
                    "expected_interval_seconds": 300,
                    "plausibility": {"min": 0, "max": 100},
                }
            }
        )
        device = self.create_device(template)
        source = self.create_source(
            device,
            "active_power",
            quantity=ImpactDataSource.QuantityKind.POWER,
            aggregation=ImpactDataSource.Aggregation.INSTANTANEOUS,
            unit="kW",
        )
        start = datetime(2026, 7, 1, tzinfo=UTC)
        for minute, value in ((0, 10), (5, 10), (10, 1000), (15, 10)):
            TelemetryData.objects.create(
                device=device,
                timestamp=start + timedelta(minutes=minute),
                key="active_power",
                value_numeric=value,
            )

        result = integrate_power_source(source, start, start + timedelta(minutes=16))

        self.assertEqual(result["rejected_samples"], 2)
        self.assertLess(result["coverage_pct"], Decimal("80"))


class ColdChainCalculationTests(ImpactTestCase):
    profile_key = "cold_chain"

    def test_excursion_delay_and_expired_calibration_lower_confidence(self):
        template = self.create_template(
            {
                "temperature": {
                    "unit": "°C",
                    "expected_interval_seconds": 300,
                    "plausibility": {"min": -50, "max": 50},
                }
            },
            device_type="temp_sensor",
        )
        device = self.create_device(template)
        self.create_source(
            device,
            "temperature",
            quantity=ImpactDataSource.QuantityKind.TEMPERATURE,
            aggregation=ImpactDataSource.Aggregation.INSTANTANEOUS,
            unit="°C",
            role=ImpactDataSource.SourceRole.MONITORING_ONLY,
            include=False,
            calibration=ImpactDataSource.CalibrationStatus.EXPIRED,
        )
        assumption = self.update_assumptions(
            cold_min_temperature=Decimal("2"),
            cold_max_temperature=Decimal("8"),
            excursion_delay_minutes=10,
            cold_loss_per_critical_excursion=Decimal("1000"),
            cold_critical_duration_minutes=30,
        )
        start = datetime(2026, 7, 1, tzinfo=UTC)
        for minute, value in ((0, 4), (5, 10), (10, 10), (15, 10), (20, 4), (25, 4)):
            TelemetryData.objects.create(
                device=device,
                timestamp=start + timedelta(minutes=minute),
                key="temperature",
                value_numeric=value,
            )

        metrics, warnings = calculate_cold_chain_metrics(
            self.profile,
            assumption,
            start,
            start + timedelta(minutes=25, seconds=1),
        )
        by_key = {metric.key: metric for metric in metrics}

        self.assertEqual(by_key["temperature_excursions"].value, Decimal("1"))
        self.assertEqual(
            by_key["time_in_range_pct"].confidence,
            ImpactMetricSnapshot.Confidence.LOW,
        )
        self.assertIn("Calibration has expired.", warnings)

    def test_assumption_revision_is_immutable(self):
        revision = self.update_assumptions()
        revision.tariff_per_kwh = Decimal("0.50")
        with self.assertRaises(ValidationError):
            revision.save()
