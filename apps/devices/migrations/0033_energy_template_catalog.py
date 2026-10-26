from django.db import migrations, models
import django.db.models


class Migration(migrations.Migration):
    dependencies = [("devices", "0032_gateway_interface_diagnostics")]

    operations = [
        migrations.AddField(model_name="devicetemplate", name="catalog_key", field=models.CharField(blank=True, max_length=120, null=True)),
        migrations.AddField(model_name="devicetemplate", name="catalog_revision", field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="devicetemplate", name="is_current", field=models.BooleanField(default=True)),
        migrations.AddField(
            model_name="devicetemplate", name="evidence_status",
            field=models.CharField(
                choices=[
                    ("legacy_unreviewed", "Legacy · review required"),
                    ("documented", "Documented draft"),
                    ("field_verified", "Field verified"),
                    ("retired", "Retired"),
                ],
                default="legacy_unreviewed", max_length=20,
            ),
        ),
        migrations.AddField(model_name="devicetemplate", name="document_revision", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="devicetemplate", name="compatibility_notes", field=models.TextField(blank=True)),
        migrations.AddConstraint(
            model_name="devicetemplate",
            constraint=models.UniqueConstraint(
                fields=("catalog_key", "catalog_revision"),
                condition=django.db.models.Q(catalog_key__isnull=False),
                name="unique_device_template_catalog_revision",
            ),
        ),
    ]
