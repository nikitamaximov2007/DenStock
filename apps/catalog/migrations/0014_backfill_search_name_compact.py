"""Заполнение поисковой формы названия без разделителей.

Свёртка считается в Python, а не одним `UPDATE ... lower(...)`: регистр по
правилам Unicode и «ё»→«е» база в локали `C` не делает вовсе (см.
`apps.core.search_text`). Поэтому строки идут пачками через `bulk_update` —
это разовая операция миграции, а не путь поиска: поиск после неё сравнивает
уже готовую проиндексированную колонку.
"""
from django.db import migrations

from apps.core.search_text import compact_search_text

BATCH = 5000


def fill(apps, schema_editor):
    PartType = apps.get_model("catalog", "PartType")
    batch = []
    queryset = PartType.objects.all().only("pk", "name").order_by("pk").iterator(chunk_size=BATCH)
    for row in queryset:
        value = compact_search_text(row.name)[:200]
        if row.search_name_compact != value:
            row.search_name_compact = value
            batch.append(row)
        if len(batch) >= BATCH:
            PartType.objects.bulk_update(batch, ["search_name_compact"])
            batch.clear()
    if batch:
        PartType.objects.bulk_update(batch, ["search_name_compact"])


def clear(apps, schema_editor):
    apps.get_model("catalog", "PartType").objects.update(search_name_compact="")


class Migration(migrations.Migration):
    dependencies = [("catalog", "0013_parttype_search_name_compact_and_more")]
    operations = [migrations.RunPython(fill, clear)]
