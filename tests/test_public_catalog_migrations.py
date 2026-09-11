"""Catalog migrations 0008-0011: upgrade data safely, keep the old release writable."""

import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("catalog", "0007_search_trigram")]


def _leaf():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_upgrade_backfills_unique_public_ids_and_publishes_nothing():
    leaf = _leaf()
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        old = executor.loader.project_state(BEFORE).apps
        category = old.get_model("catalog", "Category").objects.create(name="Old release")
        # A transactional test before this one may have flushed the seeded units.
        unit = old.get_model("catalog", "Unit").objects.get_or_create(
            name="Штука", defaults={"short_name": "шт"}
        )[0]
        OldPart = old.get_model("catalog", "PartType")
        for index in range(40):
            OldPart.objects.create(name=f"Old part {index}", category=category, unit=unit)

        MigrationExecutor(connection).migrate(leaf)

        with connection.cursor() as cursor:
            cursor.execute("SELECT public_id, is_public FROM catalog_parttype")
            rows = cursor.fetchall()
            cursor.execute("SELECT count(*) FROM catalog_publicpartphoto")
            photos = cursor.fetchone()[0]
        public_ids = [row[0] for row in rows]
        assert len(rows) == 40
        assert all(public_ids) and len(set(public_ids)) == 40
        assert all(uuid.UUID(str(value)) for value in public_ids)
        assert all(row[1] in (True, 1) for row in rows)
        assert photos == 0
    finally:
        MigrationExecutor(connection).migrate(leaf)


@pytest.mark.django_db
def test_previous_release_can_still_insert_parts_and_links_after_upgrade():
    """An application-only rollback must not break creating parts or analogs."""
    if connection.vendor != "postgresql":
        pytest.skip("database defaults are a PostgreSQL release safety net")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO catalog_category (name, sort_order, is_active, created_at, updated_at) "
            "VALUES ('Old writer', 0, true, now(), now()) RETURNING id"
        )
        category = cursor.fetchone()[0]
        cursor.execute("SELECT id FROM catalog_unit WHERE name = 'Штука'")
        unit = cursor.fetchone()[0]
        ids = []
        for name in ("Old writer A", "Old writer B"):
            # Exactly the columns the release before 0008 knows about.
            cursor.execute(
                "INSERT INTO catalog_parttype (name, category_id, unit_id, tracking_mode, "
                "description, min_stock_level, is_active, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'bulk', '', 0, true, now(), now()) RETURNING id",
                [name, category, unit],
            )
            ids.append(cursor.fetchone()[0])
        cursor.execute(
            "INSERT INTO catalog_partanalog (original_id, analog_id, note, created_at) "
            "VALUES (%s, %s, '', now()) RETURNING is_confirmed, source",
            ids,
        )
        is_confirmed, source = cursor.fetchone()
        cursor.execute(
            "SELECT count(DISTINCT public_id), bool_and(is_public) FROM catalog_parttype "
            "WHERE id = ANY(%s)",
            [ids],
        )
        distinct_ids, all_public = cursor.fetchone()
    assert distinct_ids == 2 and all_public is True
    assert is_confirmed is False and source == "internal"
