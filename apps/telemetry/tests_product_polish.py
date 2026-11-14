from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.test import RequestFactory
from django.utils import timezone

from apps.devices.models import Device, Site
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_OWNER
from apps.telemetry.models import TelemetryData
from apps.telemetry.services import get_site_summary_stats
from apps.telemetry.views import device_telemetry_history_api, export_telemetry_csv
from apps.users.models import CustomUser


@pytest.mark.django_db
def test_site_energy_uses_local_counter_deltas_and_fresh_mapped_power():
    team = Team.objects.create(name="Energy fixtures", slug="energy-fixtures")
    site = Site.objects.create(team=team, name="Singapore", timezone="Asia/Singapore")
    meters = [
        Device.objects.create(team=team, site=site, name=f"Meter {n}", device_type="power_meter", protocol="modbus_tcp")
        for n in (1, 2)
    ]
    now = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)  # 02:00 on 30 October at the site.
    start = now - timedelta(hours=2)
    rows = []
    for meter, baseline, watts in zip(meters, (100, 200), (1000, 2000), strict=False):
        for n in range(13):
            rows.append(
                TelemetryData(
                    device=meter,
                    key="energy_import",
                    timestamp=start + timedelta(minutes=10 * n),
                    value_numeric=baseline + n,
                )
            )
        rows.append(
            TelemetryData(
                device=meter, key="active_power_total", timestamp=now - timedelta(minutes=1), value_numeric=watts
            )
        )
    TelemetryData.objects.bulk_create(rows)
    mapping = {"energy_import": {"unit": "kWh"}, "active_power_total": {"unit": "W"}}
    with (
        patch("apps.devices.datapoint_maps.effective_register_map", return_value=mapping),
        patch("apps.telemetry.services.timezone.now", return_value=now),
    ):
        stats = get_site_summary_stats(site)
    assert stats["daily_kwh"] == 24
    assert stats["current_power"] == 3000
    assert stats["energy_coverage_percent"] == 100

    # A reset or rollover cannot be interpreted as consumed energy.
    TelemetryData.objects.filter(device=meters[0], key="energy_import", timestamp=start + timedelta(minutes=60)).update(
        value_numeric=0
    )
    with (
        patch("apps.devices.datapoint_maps.effective_register_map", return_value=mapping),
        patch("apps.telemetry.services.timezone.now", return_value=now),
    ):
        assert get_site_summary_stats(site)["daily_kwh"] is None


@pytest.mark.django_db
def test_site_energy_does_not_fill_gaps_or_stale_demand():
    team = Team.objects.create(name="Gap fixtures", slug="gap-fixtures")
    site = Site.objects.create(team=team, name="Tokyo", timezone="Asia/Tokyo")
    meter = Device.objects.create(team=team, site=site, name="Meter", device_type="power_meter", protocol="modbus_tcp")
    now = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)  # 03:00 Tokyo local time.
    start = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    TelemetryData.objects.bulk_create(
        [
            TelemetryData(device=meter, key="energy_import", timestamp=start, value_numeric=100),
            TelemetryData(device=meter, key="energy_import", timestamp=now - timedelta(minutes=1), value_numeric=130),
            TelemetryData(device=meter, key="active_power", timestamp=now - timedelta(hours=1), value_numeric=500),
        ]
    )
    with (
        patch(
            "apps.devices.datapoint_maps.effective_register_map",
            return_value={"energy_import": {"unit": "kWh"}, "active_power": {"unit": "W"}},
        ),
        patch("apps.telemetry.services.timezone.now", return_value=now),
    ):
        stats = get_site_summary_stats(site)
    assert stats["daily_kwh"] is None
    assert stats["current_power"] is None


@pytest.mark.django_db
def test_long_history_spans_window_and_csv_exceeds_old_cap():
    user = CustomUser.objects.create_user(username="history-polish", email="history-polish@example.com", password="pwd")
    team = Team.objects.create(name="History fixtures", slug="history-fixtures")
    Membership.objects.create(team=team, user=user, role=ROLE_OWNER)
    site = Site.objects.create(team=team, name="History")
    device = Device.objects.create(team=team, site=site, name="Sensor", device_type="sensor", protocol="modbus_tcp")
    now = timezone.now()
    TelemetryData.objects.bulk_create(
        [
            TelemetryData(
                device=device,
                key="temperature",
                timestamp=now - timedelta(days=7) + timedelta(minutes=5 + i * 2),
                value_numeric=float(i),
            )
            for i in range(5002)
        ],
        batch_size=1000,
    )
    request = RequestFactory().get(f"/a/{team.slug}/telemetry/api/history/{device.pk}/?key=temperature&hours=168")
    request.user, request.team = user, team
    import json

    data = json.loads(device_telemetry_history_api(request, team.slug, device.pk).content)
    assert data["sample_count"] > 100
    assert datetime.fromisoformat(data["first_sample_at"]) < now - timedelta(days=6)
    assert datetime.fromisoformat(data["last_sample_at"]) > now - timedelta(hours=4)

    export_request = RequestFactory().get(f"/a/{team.slug}/telemetry/api/export/{device.pk}/?days=7")
    export_request.user, export_request.team = user, team
    response = export_telemetry_csv(export_request, team.slug, device.pk)
    assert sum(1 for _ in response.streaming_content) == 5003
