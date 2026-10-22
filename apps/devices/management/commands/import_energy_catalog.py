"""Import immutable, OEM-documented energy maps without changing assigned devices."""

import json
import math
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.devices.models import Device, DeviceTemplate

MANIFEST = Path(__file__).resolve().parents[2] / "catalog" / "energy_v1.json"
LEGACY_ENERGY_NAMES = (
    "Schneider PM5110 Power Meter",  # initial_templates.json: incorrect TCP interface
    "Schneider PM5350",  # seed_templates.py: incorrect TCP interface and total power
    "SolarEdge SE Series Inverter",  # static value at runtime SunSpec scale-factor address
    "Eastron SDM630-Modbus",  # broad model and old unreviewed map
    "Eastron SDM120-Modbus",  # broad model and old unreviewed map
    "ABB B23 Energy Meter",  # broad model and incorrect units/type
    "Accuenergy AcuRev 1312",  # unreviewed seed map
)
IMMUTABLE_FIELDS = (
    "name",
    "manufacturer",
    "model_number",
    "device_type",
    "protocol",
    "mapping_strategy",
    "register_map",
    "default_polling_interval",
    "category",
    "alert_presets",
    "source",
    "source_url",
    "document_revision",
    "compatibility_notes",
)
DATA_WIDTHS = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2, "uint64": 4, "int64": 4, "float64": 4}


def _validate_manifest(payload):
    if payload.get("schema_version") != 1 or payload.get("catalog") != "energy":
        raise CommandError("Unsupported energy catalog schema")
    templates = payload.get("templates")
    if not isinstance(templates, list) or not templates:
        raise CommandError("Energy catalog has no templates")
    seen = set()
    for item in templates:
        identity = (item.get("catalog_key"), item.get("catalog_revision"))
        if not isinstance(identity[0], str) or not identity[0] or not isinstance(identity[1], int) or identity[1] < 1:
            raise CommandError(f"Invalid catalog identity: {identity}")
        if identity in seen:
            raise CommandError(f"Duplicate catalog revision: {identity}")
        seen.add(identity)
        if item.get("protocol") not in {"modbus_rtu", "modbus_tcp"}:
            raise CommandError(f"Unsupported protocol for {identity}")
        if not str(item.get("source_url", "")).startswith("https://"):
            raise CommandError(f"Missing OEM source URL for {identity}")
        if item.get("word_order") != "BIG":
            raise CommandError(f"Gateway-default word order is not supported for {identity}")
        signals = item.get("register_map")
        if not isinstance(signals, dict) or not 1 <= len(signals) <= 20:
            raise CommandError(f"Expected 1–20 signals for {identity}")
        for key, signal in signals.items():
            data_type = signal.get("type")
            address = signal.get("address")
            scale = signal.get("scale")
            if (
                data_type not in DATA_WIDTHS
                or signal.get("objectsCount") != DATA_WIDTHS[data_type]
                or signal.get("functionCode") not in {3, 4}
                or not isinstance(address, int)
                or not 0 <= address <= 65535
                or not isinstance(scale, int | float)
                or not math.isfinite(scale)
                or scale == 0
                or "unit" not in signal
                or signal.get("writable")
                or signal.get("control")
                or signal.get("readFunctionCode")
                or signal.get("writeFunctionCode")
            ):
                raise CommandError(f"Unsafe or incomplete signal {identity}/{key}")
            invalid_values = signal.get("invalidValues", [])
            if not isinstance(invalid_values, list) or any(not isinstance(value, int) for value in invalid_values):
                raise CommandError(f"Invalid sentinel list for {identity}/{key}")
    return templates


def _template_values(item):
    return {
        "name": item["name"],
        "manufacturer": item["manufacturer"],
        "model_number": item["model_number"],
        "device_type": "power_meter",
        "protocol": item["protocol"],
        "mapping_strategy": "fixed",
        "register_map": item["register_map"],
        "default_polling_interval": 30,
        "category": "energy",
        "alert_presets": [],
        "source": "curated",
        "source_url": item["source_url"],
        "document_revision": item["document_revision"],
        "compatibility_notes": f"{item['firmware']}. {item['address_convention']}. {item['configuration_notes']}",
    }


class Command(BaseCommand):
    help = "Audit legacy energy maps and import versioned, read-only documented drafts."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report changes and assignments without writing")

    @transaction.atomic
    def handle(self, *args, **options):
        templates = _validate_manifest(json.loads(MANIFEST.read_text()))
        planned = []
        for item in templates:
            values = _template_values(item)
            existing = DeviceTemplate.objects.filter(
                catalog_key=item["catalog_key"], catalog_revision=item["catalog_revision"]
            ).first()
            if existing:
                drift = [field for field in IMMUTABLE_FIELDS if getattr(existing, field) != values[field]]
                if drift:
                    identity = f"{item['catalog_key']} r{item['catalog_revision']}"
                    raise CommandError(
                        f"Published revision changed: {identity} ({', '.join(drift)}). Create a new revision instead."
                    )
                planned.append(("existing", item, values))
            else:
                planned.append(("create", item, values))

        legacy = list(
            DeviceTemplate.objects.filter(
                name__in=LEGACY_ENERGY_NAMES,
                catalog_key__isnull=True,
                created_by_team__isnull=True,
            ).order_by("id")
        )
        for old in legacy:
            assignments = Device.objects.filter(template=old).count()
            self.stdout.write(
                f"Legacy {old.name}: {assignments} assigned device(s); retire from selection, keep map and FK"
            )
        for action, item, _ in planned:
            self.stdout.write(f"{action}: {item['catalog_key']} r{item['catalog_revision']}")
        if options["dry_run"]:
            self.stdout.write("Dry run: no database changes")
            return

        for old in legacy:
            old.is_current = False
            old.evidence_status = "retired"
            old.is_verified = False
            old.save(update_fields=["is_current", "evidence_status", "is_verified"])
        for action, item, values in planned:
            if action == "create":
                DeviceTemplate.objects.create(
                    catalog_key=item["catalog_key"],
                    catalog_revision=item["catalog_revision"],
                    evidence_status="documented",
                    is_current=True,
                    is_verified=False,
                    **values,
                )
        for key in {item["catalog_key"] for item in templates}:
            newest = max(DeviceTemplate.objects.filter(catalog_key=key).values_list("catalog_revision", flat=True))
            DeviceTemplate.objects.filter(catalog_key=key).exclude(catalog_revision=newest).update(is_current=False)
        self.stdout.write(
            self.style.SUCCESS("Energy catalog imported; existing device template assignments were not changed.")
        )
