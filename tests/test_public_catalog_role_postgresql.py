"""PostgreSQL: the public catalog role can read the catalog and nothing else.

The role script is applied inside the test transaction (roles and grants are
transactional in PostgreSQL, so nothing survives the test), then the same
connection switches to the restricted role with SET LOCAL ROLE. Every public
page and service call below therefore runs with the production grant set,
and every forbidden write or read must be refused by PostgreSQL itself.
"""

import re
import uuid
from decimal import Decimal

import pytest
from django.conf import settings
from django.db import connection, transaction
from django.db.utils import ProgrammingError

from apps.catalog.public_catalog import cards_by_id, part_relations, search_catalog
from apps.catalog.public_photos import primary_photos, publish_photo, reject_photo, rendition_for

pytestmark = pytest.mark.postgresql

ROLE_SCRIPT = settings.BASE_DIR / "scripts" / "operations" / "create_public_catalog_role.sql"


@pytest.fixture
def restricted_role(db):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL role grants need PostgreSQL")
    role = f"public_test_{uuid.uuid4().hex[:10]}"
    body = ROLE_SCRIPT.read_text()
    block = body[body.index("DO $$") :]
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('denstock.public_role', %s, false)", [role])
        cursor.execute(block)
    yield role
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")


def _as(role):
    with connection.cursor() as cursor:
        cursor.execute(f'SET LOCAL ROLE "{role}"')


def _reset():
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")


def _refused(sql, params=None):
    with pytest.raises(ProgrammingError, match="permission denied"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(sql, params or [])


@pytest.fixture
def seeded(public_catalog):
    original = public_catalog.part(
        "PISTON ASSY", article="420892388", maker="BRP", russian="Поршень в сборе"
    )
    analog = public_catalog.part("PISTON KIT", article="010-921", maker="WSM")
    public_catalog.stock(original, "3")
    public_catalog.analog(original, analog)
    published = publish_photo(public_catalog.image(original), source="own", by=public_catalog.user)
    rejected_image = public_catalog.image(original)
    rejected = publish_photo(rejected_image, source="own", by=public_catalog.user)
    reject_photo(rejected_image, by=public_catalog.user)
    return {"original": original, "analog": analog, "published": published, "rejected": rejected}


def test_public_reads_work_under_the_restricted_role(restricted_role, seeded, public_client):
    original = seeded["original"]
    _as(restricted_role)

    result = search_catalog("piston", {"relation": "analog"})
    typo = search_catalog("pistn", {})
    card = cards_by_id([original.pk])[original.pk]
    relations = part_relations(original.pk)
    photos = primary_photos([original.pk])
    rendition = rendition_for(seeded["published"].public_id, "card")

    assert [c.facts.public_id for c in result.cards] == [seeded["analog"].public_id]
    assert typo.total >= 1
    assert card.facts.available_quantity == Decimal("3") and card.facts.russian_name
    assert [c.facts.public_id for c in relations.analogs] == [seeded["analog"].public_id]
    assert photos[original.pk].public_id == seeded["published"].public_id
    assert rendition is not None

    for path in (
        "/",
        "/search/?q=420-892-388",
        "/search/?q=piston&relation=original&in_stock=1",
        f"/parts/{original.public_id}/",
        f"/photos/{seeded['published'].public_id}/detail.jpg",
        "/cart/",
        "/robots.txt",
        "/sitemap.xml",
        "/sitemaps/parts-1.xml",
        "/healthz/",
    ):
        assert public_client.get(path).status_code == 200, path
    added = public_client.post(f"/cart/{original.public_id}/add/", {"quantity": "2"})
    assert added.status_code == 302
    assert "Поршень в сборе" in public_client.get("/cart/").content.decode()
    _reset()


def test_rejected_photos_are_invisible_to_the_role_even_by_direct_sql(restricted_role, seeded):
    _as(restricted_role)
    with connection.cursor() as cursor:
        cursor.execute("SELECT status FROM catalog_publicpartphoto")
        statuses = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT count(*) FROM catalog_publicpartphotorendition WHERE photo_id = %s",
            [seeded["rejected"].pk],
        )
        rejected_renditions = cursor.fetchone()[0]
    _reset()
    assert statuses == {"published"}
    assert rejected_renditions == 0
    assert rendition_for(seeded["rejected"].public_id, "card") is None


def test_other_roles_still_see_every_photo_row(restricted_role, seeded):
    """RLS narrows only the public role; the internal runtime need not own the table."""
    other = f"internal_probe_{uuid.uuid4().hex[:8]}"
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE ROLE "{other}" NOLOGIN')
        cursor.execute(f'GRANT SELECT ON catalog_publicpartphoto TO "{other}"')
        cursor.execute(f'GRANT SELECT ON catalog_publicpartphotorendition TO "{other}"')
    _as(other)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(DISTINCT status) FROM catalog_publicpartphoto")
        statuses = cursor.fetchone()[0]
    _reset()
    assert statuses == 2


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE catalog_parttype SET recommended_price = 1",
        "DELETE FROM catalog_partanalog",
        "INSERT INTO catalog_publicpartphoto (public_id) VALUES (gen_random_uuid())",
        "UPDATE catalog_publicpartphoto SET status = 'published'",
        "UPDATE inventory_stocklot SET quantity = 999",
        "DELETE FROM inventory_stocklot",
        "INSERT INTO sales_reservation (status) VALUES ('active')",
        "UPDATE sales_reservationline SET quantity = 0",
        "UPDATE actions_partcustomsinfo SET customs_name_ru_confirmed = true",
        "SELECT * FROM accounts_user",
        "SELECT * FROM customers_customer",
        "SELECT * FROM sales_sale",
        "SELECT * FROM sales_saleline",
        "SELECT * FROM repairs_repairorder",
        "SELECT * FROM procurement_batchline",
        "SELECT * FROM suppliers_supplier",
        "SELECT * FROM catalog_parttypeimage",
        "SELECT * FROM django_session",
        "SELECT * FROM inventory_stockbalance",
        "CREATE TABLE public_role_probe (id int)",
    ],
)
def test_the_role_cannot_write_or_read_outside_the_catalog(restricted_role, seeded, sql):
    _as(restricted_role)
    try:
        _refused(sql)
    finally:
        _reset()


def test_the_role_script_grants_exactly_the_documented_tables(restricted_role):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = %s",
            [restricted_role],
        )
        grants = cursor.fetchall()
    assert {privilege for _table, privilege in grants} == {"SELECT"}
    grant_clause = ROLE_SCRIPT.read_text().split("GRANT SELECT ON TABLE", 1)[1]
    documented = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", grant_clause.split("TO %I", 1)[0]))
    assert {table for table, _privilege in grants} == documented
