"""Restrict the public RU trigram index to operator-confirmed names.

Public catalog search has always constrained the RU branch to confirmed
customs names.  Making the same predicate part of the GIN index prevents a
large population of generated, unconfirmed translations from becoming fuzzy
candidates before that constraint is applied.
"""

from django.db import migrations

INDEX = "actions_partcustomsinfo_ru_upper_trgm"
CREATE = (
    f"CREATE INDEX IF NOT EXISTS {INDEX} "
    "ON actions_partcustomsinfo USING gin (UPPER(customs_name_ru::text) gin_trgm_ops) "
    "WHERE customs_name_ru_confirmed"
)


def replace_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX}")
        schema_editor.execute(CREATE)


def restore_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX}")
        schema_editor.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX} "
            "ON actions_partcustomsinfo USING gin (UPPER(customs_name_ru::text) gin_trgm_ops)"
        )


class Migration(migrations.Migration):

    dependencies = [("actions", "0013_customs_name_ru_trigram")]

    operations = [migrations.RunPython(replace_index, restore_index)]
