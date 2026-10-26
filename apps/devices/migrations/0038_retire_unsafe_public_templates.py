from django.db import migrations


def retire_unsafe_public_templates(apps, schema_editor):
    """Keep assigned revisions, but remove unreviewed public seeds from new selection."""
    Template = apps.get_model("devices", "DeviceTemplate")
    for template in Template.objects.filter(created_by_team__isnull=True, is_current=True).iterator():
        signals = template.register_map or {}
        has_write = any(
            isinstance(point, dict) and (
                point.get("writable") or point.get("writeFunctionCode")
                or point.get("functionCode") in {5, 6, 15, 16}
            )
            for point in signals.values()
        ) if isinstance(signals, dict) else True
        unsafe = (
            template.source not in {"curated", "community"}
            or template.evidence_status not in {"documented", "field_verified"}
            or not template.manufacturer.strip() or not template.model_number.strip()
            or not template.source_url.startswith("https://")
            or not signals or has_write
            or (template.device_type == "plc" and not template.program_scope.strip())
        )
        if unsafe:
            Template.objects.filter(pk=template.pk).update(
                is_current=False, is_verified=False, evidence_status="retired"
            )


class Migration(migrations.Migration):
    dependencies = [("devices", "0037_devicetemplate_model_aliases_devicetemplate_variant_and_more")]
    operations = [migrations.RunPython(retire_unsafe_public_templates, migrations.RunPython.noop)]
