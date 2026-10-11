from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("devices", "0031_durable_discovery_candidates")]

    operations = [
        migrations.AddField(
            model_name="gateway",
            name="interface_diagnostics",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
