from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("warehouse", "0006_retire_reused_location_aliases"),
        ("writeoffs", "0003_writeoffdocument_business_author"),
    ]

    operations = [
        migrations.AddField(
            model_name="writeoffline",
            name="source_status",
            field=models.CharField(blank=True, editable=False, max_length=20, verbose_name="Исходный статус для отмены"),
        ),
        migrations.AddField(
            model_name="writeoffline",
            name="source_location",
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="warehouse.storagelocation", verbose_name="Исходная ячейка для отмены"),
        ),
    ]
