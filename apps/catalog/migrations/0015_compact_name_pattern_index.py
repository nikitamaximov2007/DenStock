"""Индекс под префиксный поиск по свёрнутому названию.

Обычный btree по `search_name_compact` обслуживает только равенство: `LIKE
'x%'` использует индекс, лишь если он построен с `varchar_pattern_ops` (или
вся база в локали `C`). Без него префиксный тир на 272k строк превращается в
Seq Scan. Проверено на PostgreSQL 16: с обычным индексом план префикса —
Seq Scan, с pattern_ops — Index Scan.

Подстрочный тир (`LIKE '%x%'`) префиксным индексом не ускоряется в принципе;
его защищает порог длины и общий предел выдачи, как и раньше.
"""
from django.db import migrations

INDEX = "parttype_name_compact_pattern_idx"
CREATE = (
    f"CREATE INDEX IF NOT EXISTS {INDEX} "
    "ON catalog_parttype (search_name_compact varchar_pattern_ops)"
)


def create(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE)


def drop(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX}")


class Migration(migrations.Migration):
    dependencies = [("catalog", "0014_backfill_search_name_compact")]
    operations = [migrations.RunPython(create, drop)]
