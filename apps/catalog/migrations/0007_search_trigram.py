"""Trigram support for shared part search (Public Catalog Stage 2).

PostgreSQL only. On any other backend every operation here is a no-op, so the
SQLite test suite and local development are unaffected.

* ``pg_trgm`` gives the ``%`` similarity operator and lets GIN indexes answer
  ``LIKE``/``ILIKE`` prefix and substring predicates.
* ``catalog_partnumber (normalized_value)`` serves article prefix/substring.
* ``catalog_parttype (UPPER(name::text))`` serves the English name tiers. The
  expression matches exactly what Django emits for ``iexact``/``istartswith``/
  ``icontains`` on PostgreSQL, which is what lets the planner use it.

Every statement is idempotent (``IF NOT EXISTS`` / ``IF EXISTS``), so a
redeploy or a re-run after a partial failure is safe. Reversing drops the
indexes and then, through Django's ``TrigramExtension``, the ``pg_trgm``
extension as well - verified on PostgreSQL 16. Rollback therefore returns the
schema exactly to its Stage 1 state.
"""
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations

INDEXES = (
    (
        "catalog_partnumber_normalized_trgm",
        "CREATE INDEX IF NOT EXISTS catalog_partnumber_normalized_trgm "
        "ON catalog_partnumber USING gin (normalized_value gin_trgm_ops)",
    ),
    (
        "catalog_parttype_name_upper_trgm",
        "CREATE INDEX IF NOT EXISTS catalog_parttype_name_upper_trgm "
        "ON catalog_parttype USING gin (UPPER(name::text) gin_trgm_ops)",
    ),
)


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for _name, sql in INDEXES:
        schema_editor.execute(sql)


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for name, _sql in INDEXES:
        schema_editor.execute(f"DROP INDEX IF EXISTS {name}")


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0006_alter_partnumber_kind"),
    ]

    operations = [
        TrigramExtension(),
        migrations.RunPython(create_indexes, drop_indexes),
    ]
