import uuid

from django.db import migrations, models


def backfill_public_ids(apps, schema_editor):
    PartType = apps.get_model("catalog", "PartType")
    for part in PartType.objects.filter(public_id__isnull=True).iterator():
        part.public_id = uuid.uuid4()
        part.save(update_fields=["public_id"])


class Migration(migrations.Migration):
    dependencies = [("catalog", "0007_search_trigram")]

    operations = [
        migrations.AddField(
            model_name="parttype",
            name="public_id",
            field=models.UUIDField("Публичный ID", editable=False, null=True, unique=True),
        ),
        migrations.RunPython(backfill_public_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="parttype",
            name="public_id",
            field=models.UUIDField(
                "Публичный ID", default=uuid.uuid4, editable=False, unique=True
            ),
        ),
        migrations.AddField(
            model_name="parttype",
            name="is_public",
            field=models.BooleanField(default=True, verbose_name="Показывать в публичном каталоге"),
        ),
    ]
