"""Seed the Modbus TCP map used by Laptop 2's cold-room test simulator."""

from django.core.management.base import BaseCommand, CommandError

from apps.devices.models import DeviceTemplate
from apps.teams.models import Team

TEMPLATE_NAME = "Test simulator · Novena Cold Room NCS-100 (TCP)"
REGISTER_MAP = {
    "temperature": {
        "label": "Temperature",
        "unit": "degC",
        "type": "float32",
        "address": 3000,
        "functionCode": 3,
        "dashboard_role": "primary",
        "normal_min": 0,
        "normal_max": 4,
    },
    "humidity": {
        "label": "Humidity",
        "unit": "%",
        "type": "float32",
        "address": 3002,
        "functionCode": 3,
        "dashboard_role": "secondary",
    },
    "door_open": {
        "label": "Door Open",
        "type": "bool",
        "address": 10,
        "functionCode": 1,
        "dashboard_role": "health",
        "labels": ["Closed", "Open"],
    },
    "compressor_status": {
        "label": "Compressor",
        "type": "bool",
        "address": 11,
        "functionCode": 1,
        "dashboard_role": "health",
        "labels": ["Off", "Running"],
    },
}


class Command(BaseCommand):
    help = "Add the unverified cold-room Modbus TCP simulator map to one pilot team's picker."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Slug of the local pilot team")

    def handle(self, *args, **options):
        team = Team.objects.filter(slug=options["team"]).first()
        if team is None:
            raise CommandError(f"Team '{options['team']}' does not exist.")

        _, created = DeviceTemplate.objects.get_or_create(
            created_by_team=team,
            name=TEMPLATE_NAME,
            defaults={
                "manufacturer": "Novena",
                "model_number": "NCS-100",
                "device_type": "temp_sensor",
                "protocol": "modbus_tcp",
                "mapping_strategy": "fixed",
                "register_map": REGISTER_MAP,
                "category": "cold_chain",
                "alert_presets": [],
                "is_verified": False,
                "source": "user_created",
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{TEMPLATE_NAME} {'added' if created else 'already present'} for {team.slug}. "
                "This test map must pass live validation before deployment."
            )
        )
