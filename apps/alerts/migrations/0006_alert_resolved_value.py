from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0005_alertrule_create_maintenance_ticket_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="alert",
            name="resolved_value",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
