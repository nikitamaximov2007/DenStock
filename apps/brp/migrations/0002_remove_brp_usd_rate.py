from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("brp", "0001_initial"),
        ("warehouse", "0003_unified_current_usd_rate"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="brppricingsettings",
            name="brp_usd_rate",
        ),
    ]
