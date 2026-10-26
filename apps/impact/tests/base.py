from decimal import Decimal

from django.test import TestCase

from apps.devices.models import Device, DeviceTemplate, Site
from apps.impact.models import ImpactAssumptionRevision, ImpactDataSource
from apps.impact.services import ensure_business_profile, ensure_site_profile
from apps.teams.models import Team


class ImpactTestCase(TestCase):
    profile_key = "factory_energy"

    def setUp(self):
        self.team = Team.objects.create(name="Impact Team", slug="impact-team")
        self.site = Site.objects.create(
            team=self.team,
            name="Main Site",
            timezone="Asia/Singapore",
            solution_profile=self.profile_key,
        )
        self.profile = ensure_site_profile(self.site)
        self.business = ensure_business_profile(self.team)

    def create_template(self, register_map, *, device_type="power_meter"):
        return DeviceTemplate.objects.create(
            name=f"Verified {device_type}",
            manufacturer="Novena Test",
            model_number=f"M-{DeviceTemplate.objects.count() + 1}",
            device_type=device_type,
            protocol="modbus_tcp",
            register_map=register_map,
            default_polling_interval=300,
            is_verified=True,
            evidence_status="field_verified",
        )

    def create_device(
        self,
        template,
        *,
        name="Meter",
        energy_category="consumption",
    ):
        return Device.objects.create(
            team=self.team,
            site=self.site,
            template=template,
            name=name,
            device_type=template.device_type,
            protocol=template.protocol,
            energy_category=energy_category,
        )

    def create_source(
        self,
        device,
        key,
        *,
        quantity=ImpactDataSource.QuantityKind.ENERGY,
        aggregation=ImpactDataSource.Aggregation.CUMULATIVE_COUNTER,
        unit="kWh",
        role=ImpactDataSource.SourceRole.SITE_BOUNDARY,
        include=True,
        calibration=ImpactDataSource.CalibrationStatus.NOT_APPLICABLE,
    ):
        return ImpactDataSource.objects.create(
            team=self.team,
            site_profile=self.profile,
            device=device,
            telemetry_key=key,
            quantity_kind=quantity,
            aggregation=aggregation,
            canonical_unit=unit,
            conversion_factor=Decimal("1"),
            source_role=role,
            include_in_totals=include,
            verification_status=ImpactDataSource.VerificationStatus.CONFIRMED,
            calibration_status=calibration,
        )

    def update_assumptions(self, **values):
        latest = self.profile.assumption_revisions.order_by("-revision").first()
        defaults = {
            "currency": "SGD",
            "tariff_per_kwh": Decimal("0.28"),
            "expected_after_hours_base_kw": Decimal("0"),
        }
        defaults.update(values)
        return ImpactAssumptionRevision.objects.create(
            team=self.team,
            site_profile=self.profile,
            revision=latest.revision + 1,
            **defaults,
        )
