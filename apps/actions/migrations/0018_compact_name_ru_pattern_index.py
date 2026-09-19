"""Тот же префиксный индекс для свёрнутого русского названия."""
from django.db import migrations

INDEX = "customs_name_ru_compact_pattern_idx"
CREATE = (
    f"CREATE INDEX IF NOT EXISTS {INDEX} "
    "ON actions_partcustomsinfo (search_name_ru_compact varchar_pattern_ops) "
    "WHERE customs_name_ru_confirmed"
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
    dependencies = [("actions", "0017_backfill_search_name_ru_compact")]
    operations = [migrations.RunPython(create, drop)]
