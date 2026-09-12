"""Поисковая форма русского таможенного названия.

Регистр кириллицы сворачивается в Python, а не через `UPPER()` в базе: `UPPER`
считает регистр по локали кластера и в локали `C` кириллицу не трогает вовсе,
из-за чего поиск по русскому названию был регистрозависимым (см.
`apps/core/search_text.py`). Поэтому рядом с названием появляется свёрнутая
форма, и поиск идёт по ней обычным сравнением и триграммами.

Триграммный индекс переезжает с `UPPER(customs_name_ru)` на новую колонку:
старое выражение после этой миграции не встречается ни в одном запросе.
Предикат «только подтверждённые» сохраняется - публичный поиск и операторский
поиск читают лишь подтверждённые названия.
"""

from django.db import migrations, models

from apps.core.search_text import fold_search_text

OLD_INDEX = "actions_partcustomsinfo_ru_upper_trgm"
NEW_INDEX = "actions_partcustomsinfo_search_ru_trgm"
CREATE_OLD = (
    f"CREATE INDEX IF NOT EXISTS {OLD_INDEX} "
    "ON actions_partcustomsinfo USING gin (UPPER(customs_name_ru::text) gin_trgm_ops) "
    "WHERE customs_name_ru_confirmed"
)
CREATE_NEW = (
    f"CREATE INDEX IF NOT EXISTS {NEW_INDEX} "
    "ON actions_partcustomsinfo USING gin (search_name_ru gin_trgm_ops) "
    "WHERE customs_name_ru_confirmed"
)


def fill_search_names(apps, schema_editor):
    """Свернуть уже сохранённые названия. Пачками: таблица может быть большой."""
    model = apps.get_model("actions", "PartCustomsInfo")
    rows = model.objects.exclude(customs_name_ru="").only("id", "customs_name_ru")
    batch = []
    for row in rows.iterator(chunk_size=2000):
        row.search_name_ru = fold_search_text(row.customs_name_ru)[:255]
        batch.append(row)
        if len(batch) >= 2000:
            model.objects.bulk_update(batch, ["search_name_ru"])
            batch = []
    if batch:
        model.objects.bulk_update(batch, ["search_name_ru"])


def clear_search_names(apps, schema_editor):
    apps.get_model("actions", "PartCustomsInfo").objects.update(search_name_ru="")


def move_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"DROP INDEX IF EXISTS {OLD_INDEX}")
    schema_editor.execute(CREATE_NEW)


def restore_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"DROP INDEX IF EXISTS {NEW_INDEX}")
    schema_editor.execute(CREATE_OLD)


class Migration(migrations.Migration):

    dependencies = [
        ("actions", "0014_confirmed_customs_name_ru_trigram"),
    ]

    operations = [
        migrations.AddField(
            model_name="partcustomsinfo",
            name="search_name_ru",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=255,
                verbose_name="Русское название для поиска",
            ),
        ),
        migrations.AddIndex(
            model_name="partcustomsinfo",
            index=models.Index(
                fields=["search_name_ru"], name="customs_search_name_ru_idx"
            ),
        ),
        migrations.RunPython(fill_search_names, clear_search_names),
        migrations.RunPython(move_index, restore_index),
    ]
