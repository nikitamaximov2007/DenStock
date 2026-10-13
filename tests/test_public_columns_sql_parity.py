"""The hidden-column list in Python equals the one in the role script."""

import re

from django.conf import settings

from apps.inventory.public_columns import HIDDEN_COLUMNS

ROLE_SCRIPT = settings.BASE_DIR / "scripts" / "operations" / "create_public_catalog_role.sql"


def test_sql_and_python_hide_exactly_the_same_columns():
    body = ROLE_SCRIPT.read_text()
    block = body.split("FOR restricted_table, hidden_columns IN SELECT * FROM (VALUES", 1)[1]
    block = block.split(") AS restricted(name, hidden)", 1)[0]
    sql = {
        table: tuple(re.findall(r"'([a-z_]+)'", columns))
        for table, columns in re.findall(r"\('([a-z_]+)', ARRAY\[(.*?)\]\)", block, re.S)
    }
    assert sql == HIDDEN_COLUMNS


def test_hidden_tables_have_no_table_level_select():
    grant = ROLE_SCRIPT.read_text().split("'GRANT SELECT ON TABLE '", 1)[1].split("'TO %I'")[0]
    granted = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", grant))
    assert not granted & set(HIDDEN_COLUMNS)
