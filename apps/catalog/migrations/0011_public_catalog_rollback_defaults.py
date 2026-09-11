"""Database defaults that keep the previous release able to write after this one.

Migrations 0008 and 0009 add NOT NULL columns whose defaults exist only in
Django. The release before them does not know these columns, so after the
forward migration its INSERTs into ``catalog_parttype`` (create or promote a
part) and ``catalog_partanalog`` (link an analog) would fail. With these
PostgreSQL defaults an application-only rollback keeps working, without
reverting the schema and without losing public IDs or confirmations:

* ``public_id``: a new random UUID, exactly what the Django default does;
* ``is_public``: true, the model default;
* ``source``: "internal", the model default;
* ``is_confirmed``: false, so nothing the old code links becomes public.

Current code always sends these values itself; the defaults only matter for
an older writer. Django state is unchanged. SQLite, used only by tests, is
left alone.
"""

from django.db import migrations

DEFAULTS = (
    ("catalog_parttype", "public_id", "gen_random_uuid()"),
    ("catalog_parttype", "is_public", "true"),
    ("catalog_partanalog", "source", "'internal'"),
    ("catalog_partanalog", "is_confirmed", "false"),
)


def add_defaults(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, column, value in DEFAULTS:
        schema_editor.execute(
            f"ALTER TABLE {schema_editor.quote_name(table)} "
            f"ALTER COLUMN {schema_editor.quote_name(column)} SET DEFAULT {value}"
        )


def drop_defaults(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, column, _value in DEFAULTS:
        schema_editor.execute(
            f"ALTER TABLE {schema_editor.quote_name(table)} "
            f"ALTER COLUMN {schema_editor.quote_name(column)} DROP DEFAULT"
        )


class Migration(migrations.Migration):
    dependencies = [("catalog", "0010_public_part_photos")]

    operations = [migrations.RunPython(add_defaults, drop_defaults)]
