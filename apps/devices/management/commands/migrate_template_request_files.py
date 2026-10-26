"""Copy legacy public request PDFs to private storage, then remove public copies."""

import hashlib

from django.core.files.storage import default_storage, storages
from django.core.management.base import BaseCommand, CommandError

from apps.devices.models import EquipmentTemplateRequest


def digest(storage, name):
    value = hashlib.sha256()
    with storage.open(name, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.digest()


class Command(BaseCommand):
    help = "Move request manuals from public media to private storage. Use --apply after reviewing dry-run output."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        private = storages["template_request_documents"]
        pending = 0
        moved = 0
        for item in EquipmentTemplateRequest.objects.exclude(documentation_file="").iterator():
            name = item.documentation_file.name
            if not name:
                continue
            public_exists = default_storage.exists(name)
            private_exists = private.exists(name)
            if not public_exists:
                if not private_exists:
                    raise CommandError(f"Missing request manual {item.pk}: {name}")
                continue
            pending += 1
            self.stdout.write(f"{item.pk}: {name}")
            if not options["apply"]:
                continue
            if not private_exists:
                with default_storage.open(name, "rb") as source:
                    saved = private.save(name, source)
                if saved != name:
                    raise CommandError(f"Private storage changed the filename for request {item.pk}: {saved}")
            if digest(default_storage, name) != digest(private, name):
                raise CommandError(f"Copy verification failed for request {item.pk}: {name}")
            default_storage.delete(name)
            if default_storage.exists(name):
                raise CommandError(f"Public copy still exists for request {item.pk}: {name}")
            moved += 1
        self.stdout.write(self.style.SUCCESS(f"Public manuals found: {pending}; moved: {moved}."))
