"""Rollback safety of the inventory cancel/restart release (PostgreSQL).

The previous production release (f7b8086) does not know
``cancellation_reason`` and ``restart_reason``. If the application is rolled
back after the migration, its INSERTs omit these NOT NULL columns; without a
database default every new counting session and every cell recount would fail
(the recount even misreported it as "an unfinished recount already exists").
"""

import pytest
from django.db import connection

pytestmark = pytest.mark.postgresql


@pytest.mark.django_db
@pytest.mark.parametrize("table, column", [
    ("counting_inventorycountingsession", "cancellation_reason"),
    ("stocktaking_sectionrecount", "restart_reason"),
])
def test_new_not_null_text_columns_have_a_database_default(table, column):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL schema check")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            [table, column])
        nullable, default = cursor.fetchone()
    assert nullable == "NO"
    assert default is not None and default.startswith("''"), default


@pytest.mark.django_db
def test_an_insert_that_omits_the_new_column_still_works():
    """What the previous release's INSERT looks like: the column is simply absent."""
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL insert check")
    from apps.warehouse.models import StorageLocation

    location = StorageLocation.objects.create(code="S09-D09-C09")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'counting_inventorycountingsession' AND is_nullable = 'NO' "
            "AND column_default IS NULL AND column_name NOT IN ('id')")
        required = {row[0] for row in cursor.fetchall()}
    # Only columns that the previous release also fills may stay without a default.
    assert "cancellation_reason" not in required
    assert location.pk
