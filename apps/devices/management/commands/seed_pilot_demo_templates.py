"""Add safe, team-scoped equipment choices for a local onboarding walkthrough."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.devices.models import DeviceTemplate
from apps.teams.models import Team

DEMO_TEMPLATES = (
    ("Demo starter · Schneider PM5110", "Schneider Electric", "PM5110", "power_meter", "energy"),
    ("Demo starter · Delta MS300", "Delta Electronics", "MS300", "vfd", "factory"),
    ("Demo starter · Omron CP1L / CP1H", "Omron", "CP1L / CP1H", "plc", "factory"),
    ("Demo starter · Siemens S7-1200", "Siemens", "S7-1200", "plc", "factory"),
)


class Command(BaseCommand):
    help = "Add unverified Modbus TCP demo starters to one team's template picker."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Slug of the local pilot team")

    def handle(self, *args, **options):
        team = Team.objects.filter(slug=options["team"]).first()
        if team is None:
            raise CommandError(f"Team '{options['team']}' does not exist.")

        created_count = 0
        with transaction.atomic():
            for name, manufacturer, model_number, device_type, category in DEMO_TEMPLATES:
                _, created = DeviceTemplate.objects.get_or_create(
                    created_by_team=team,
                    name=name,
                    defaults={
                        "manufacturer": manufacturer,
                        "model_number": model_number,
                        "device_type": device_type,
                        "protocol": "modbus_tcp",
                        "mapping_strategy": "site_defined",
                        "register_map": {},
                        "category": category,
                        "is_verified": False,
                        "source": "user_created",
                    },
                )
                created_count += created

        self.stdout.write(
            self.style.SUCCESS(
                f"{created_count} demo starter(s) added for {team.slug}; "
                f"{len(DEMO_TEMPLATES) - created_count} already present. "
                "Each needs a site-specific signal map before live validation."
            )
        )
