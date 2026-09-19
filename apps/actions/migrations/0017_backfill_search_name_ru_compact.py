"""Та же поисковая форма для подтверждённого русского названия."""
from django.db import migrations

from apps.core.search_text import compact_search_text

BATCH = 5000


def fill(apps, schema_editor):
    PartCustomsInfo = apps.get_model("actions", "PartCustomsInfo")
    batch = []
    rows = (
        PartCustomsInfo.objects.all()
        .only("pk", "customs_name_ru")
        .order_by("pk")
        .iterator(chunk_size=BATCH)
    )
    for row in rows:
        value = compact_search_text(row.customs_name_ru)[:255]
        if row.search_name_ru_compact != value:
            row.search_name_ru_compact = value
            batch.append(row)
        if len(batch) >= BATCH:
            PartCustomsInfo.objects.bulk_update(batch, ["search_name_ru_compact"])
            batch.clear()
    if batch:
        PartCustomsInfo.objects.bulk_update(batch, ["search_name_ru_compact"])


def clear(apps, schema_editor):
    apps.get_model("actions", "PartCustomsInfo").objects.update(search_name_ru_compact="")


class Migration(migrations.Migration):
    # Таблица большая (на production ~272k видов деталей). Одна транзакция на
    # весь проход держала бы блокировки минутами, поэтому миграция неатомарна:
    # каждый `bulk_update` коммитится сам. Операция идемпотентна — значение
    # детерминированно считается из названия, — поэтому прерванный проход
    # достаточно запустить заново.
    atomic = False
    dependencies = [("actions", "0016_partcustomsinfo_search_name_ru_compact_and_more")]
    operations = [migrations.RunPython(fill, clear)]
