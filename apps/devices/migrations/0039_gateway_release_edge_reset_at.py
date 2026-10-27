from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("devices", "0038_retire_unsafe_public_templates")]

    operations = [
        migrations.AddField(
            model_name="gatewayreleaserequest",
            name="edge_reset_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
