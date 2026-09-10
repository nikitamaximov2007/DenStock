"""Trigram index on the canonical Russian customs name (Public Catalog Stage 2).

PostgreSQL only; a no-op elsewhere. Serves the Russian name tiers of shared
part search. The expression mirrors what Django emits for case-insensitive
lookups on PostgreSQL. Search reads confirmed names only; the confirmation
filter is applied in the query, not baked into the index, so the index stays
valid if a name is confirmed later.

Depends on catalog 0007, which creates the ``pg_trgm`` extension.
"""
from django.db import migrations

INDEX = "actions_partcustomsinfo_ru_upper_trgm"
CREATE = (
    f"CREATE INDEX IF NOT EXISTS {INDEX} "
    "ON actions_partcustomsinfo USING gin (UPPER(customs_name_ru::text) gin_trgm_ops)"
)


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE)


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX}")


class Migration(migrations.Migration):

    dependencies = [
        ("actions", "0012_quick_action_application_areas"),
        ("catalog", "0007_search_trigram"),
    ]

    operations = [
        migrations.RunPython(create_index, drop_index),
    ]
