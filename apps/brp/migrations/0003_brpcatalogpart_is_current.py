from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brp", "0002_remove_brp_usd_rate"),
    ]

    operations = [
        migrations.AddField(
            model_name="brpcatalogpart",
            name="is_current",
            field=models.BooleanField(db_index=True, default=True, verbose_name="В актуальном каталоге"),
        ),
    ]
