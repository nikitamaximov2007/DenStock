"""PostgreSQL: the public catalog role reads the catalog and inserts requests only.

The role script is applied inside the test transaction (roles and grants are
transactional in PostgreSQL, so nothing survives the test), then the same
connection switches to the restricted role with SET LOCAL ROLE. Every public
page and service call below therefore runs with the production grant set,
and every forbidden write or read must be refused by PostgreSQL itself.

One test runs outside a test transaction instead: it logs the whole request
flow through a session that is read-only by default, as the role's database
settings make it in deployment, with the write guard active.
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
    _apply_role_script(role)
    yield role
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")


def _apply_role_script(role):
    """The deployment identity creates the LOGIN role; the script configures it."""
    body = ROLE_SCRIPT.read_text()
    block = body[body.index("DO $$") :]
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE ROLE "{role}" NOLOGIN')
        cursor.execute("SELECT set_config('denstock.public_role', %s, false)", [role])
        cursor.execute(block)


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
        # Requests: insert only; earlier customers' contacts stay unreadable.
        "SELECT * FROM customer_requests_customerrequest",
        "SELECT customer_name, customer_phone FROM customer_requests_customerrequest",
        "SELECT comment FROM customer_requests_customerrequest",
        "SELECT * FROM customer_requests_customerrequestline",
        "UPDATE customer_requests_customerrequest SET status = 'completed'",
        "DELETE FROM customer_requests_customerrequest",
        "UPDATE customer_requests_customerrequestline SET price_seen = 0",
        "DELETE FROM customer_requests_customerrequestline",
        "SELECT * FROM customer_requests_customerrequeststatusevent",
        "SELECT * FROM customer_requests_customerrequestmessengercontact",
        "SELECT * FROM customer_requests_customerrequestmessengerlinktoken",
        "INSERT INTO customer_requests_customerrequeststatusevent (request_id) VALUES (1)",
        # Telegram rows: insert only; chats, tokens and messages stay unreadable.
        "SELECT customer_chat_id FROM customer_requests_telegramconversation",
        "UPDATE customer_requests_telegramconversation SET customer_chat_id = 1",
        "SELECT token_hash FROM customer_requests_customerrequestmessengerlinktoken",
        "UPDATE customer_requests_customerrequestmessengerlinktoken SET used_at = now()",
        "SELECT * FROM customer_requests_telegrammessage",
        "INSERT INTO customer_requests_telegrammessage (conversation_id) VALUES (1)",
        "SELECT * FROM customer_requests_telegramoperator",
        "SELECT * FROM customer_requests_telegramdelivery",
        "DELETE FROM customer_requests_telegramoutboxevent",
        "SELECT * FROM operations_telegrambotruntime",
        # The write guard's row: only the generation counter moves.
        "UPDATE operations_deploymentstate SET write_state = 'normal'",
        "SELECT database_identity FROM operations_deploymentstate",
        "DELETE FROM operations_deploymentstate",
        "SELECT * FROM django_migrations",
    ],
)
def test_the_role_cannot_write_or_read_outside_the_catalog(restricted_role, seeded, sql):
    _as(restricted_role)
    try:
        _refused(sql)
    finally:
        _reset()


def test_the_role_script_grants_exactly_the_documented_privileges(restricted_role):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = %s",
            [restricted_role],
        )
        grants = cursor.fetchall()
        cursor.execute(
            "SELECT c.relname, a.attname, acl.privilege_type FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "CROSS JOIN LATERAL aclexplode(a.attacl) acl "
            "WHERE acl.grantee = (SELECT oid FROM pg_roles WHERE rolname = %s)",
            [restricted_role],
        )
        columns = set(cursor.fetchall())
        cursor.execute(
            "SELECT unnest(setconfig) FROM pg_db_role_setting WHERE setrole = "
            "(SELECT oid FROM pg_roles WHERE rolname = %s) AND setdatabase = "
            "(SELECT oid FROM pg_database WHERE datname = current_database())",
            [restricted_role],
        )
        session_defaults = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT count(*) FROM information_schema.role_usage_grants WHERE grantee = %s",
            [restricted_role],
        )
        sequence_grants = cursor.fetchone()[0]

    assert {privilege for _table, privilege in grants} == {"SELECT", "INSERT"}
    grant_clause = ROLE_SCRIPT.read_text().split("'GRANT SELECT ON TABLE '", 1)[1]
    documented = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", grant_clause.split("'TO %I'", 1)[0]))
    assert {table for table, privilege in grants if privilege == "SELECT"} == documented
    assert {table for table, privilege in grants if privilege == "INSERT"} == {
        "customer_requests_customerrequest",
        "customer_requests_customerrequestline",
        "customer_requests_telegramconversation",
        "customer_requests_telegramoutboxevent",
        "customer_requests_customerrequestmessengerlinktoken",
    }
    assert columns == {
        ("customer_requests_customerrequest", "id", "SELECT"),
        ("customer_requests_customerrequest", "public_id", "SELECT"),
        ("customer_requests_customerrequest", "submission_key_hash", "SELECT"),
        ("customer_requests_customerrequestline", "id", "SELECT"),
        ("customer_requests_telegramconversation", "id", "SELECT"),
        ("customer_requests_telegramoutboxevent", "id", "SELECT"),
        ("customer_requests_customerrequestmessengerlinktoken", "id", "SELECT"),
        ("operations_deploymentstate", "id", "SELECT"),
        ("operations_deploymentstate", "write_state", "SELECT"),
        ("operations_deploymentstate", "business_generation", "SELECT"),
        ("operations_deploymentstate", "business_generation", "UPDATE"),
    }
    assert session_defaults == {
        "default_transaction_read_only=on",
        "statement_timeout=5s",
        "idle_in_transaction_session_timeout=30s",
    }
    assert sequence_grants == 0, "identity columns need no sequence privilege"


def test_the_role_script_refuses_a_role_that_does_not_exist(db):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL role grants need PostgreSQL")
    body = ROLE_SCRIPT.read_text()
    with pytest.raises(Exception, match="does not exist"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('denstock.public_role', 'no_such_role_x', false)")
            cursor.execute(body[body.index("DO $$") :])


def test_a_public_request_is_inserted_under_the_restricted_role(
    restricted_role, public_catalog, public_client, settings
):
    part = public_catalog.part("SEAL", article="SE-1", price="700")
    missing = public_catalog.part("IMPELLER", article="IMP-1")
    public_catalog.stock(part, "5")
    settings.DENSTOCK_MODE = "public-catalog"
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    _as(restricted_role)
    try:
        public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "2"})
        public_client.post(f"/cart/{missing.public_id}/add/", {"quantity": "1"})
        form = public_client.get("/request/").content.decode()
        token = re.search(r'name="submission_key" value="([^"]+)"', form).group(1)
        response = public_client.post(
            "/request/submit/",
            {
                "submission_key": token,
                "customer_name": "Иван",
                "customer_phone": "+7 912 123-45-67",
                "preferred_messenger": "telegram",
                "consent": "1",
            },
        )
        retry = public_client.post("/request/submit/", {"submission_key": token})
        success = public_client.get(response["Location"])
        telegram_continue = public_client.post(response["Location"] + "telegram/")
    finally:
        settings.DENSTOCK_MODE = "test"
        _reset()
    from apps.customer_requests.models import CustomerRequest

    assert response.status_code == 302 and retry["Location"] == response["Location"]
    assert success.status_code == 200
    assert telegram_continue.status_code == 302
    assert telegram_continue["Location"].startswith("https://t.me/")
    request = CustomerRequest.objects.get()
    assert {(line.part_type_id, line.is_supply_inquiry) for line in request.lines.all()} == {
        (part.pk, False),
        (missing.pk, True),
    }


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_the_request_write_works_on_a_read_only_session_with_the_guard_on(
    public_catalog, public_client, settings
):
    """As deployed: role defaults make every transaction read-only, the guard is on.

    SET ROLE does not apply a role's login defaults, so the session sets the
    same default explicitly. Everything outside the one explicit read-write
    request transaction must stay read-only.
    """
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL role grants need PostgreSQL")
    from apps.catalog.models import Unit
    from apps.customer_requests.models import CustomerRequest

    part = public_catalog.part("SEAL", article="SE-1", price="700")
    public_catalog.stock(part, "5")
    role = f"public_test_{uuid.uuid4().hex[:10]}"
    _apply_role_script(role)
    settings.DENSTOCK_MODE = "public-catalog"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET default_transaction_read_only = on")
            cursor.execute(f'SET ROLE "{role}"')
        public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
        form = public_client.get("/request/").content.decode()
        token = re.search(r'name="submission_key" value="([^"]+)"', form).group(1)
        response = public_client.post(
            "/request/submit/",
            {
                "submission_key": token,
                "customer_name": "Иван",
                "customer_phone": "+7 912 123-45-67",
                "preferred_messenger": "max",
                "consent": "1",
            },
        )
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
        with pytest.raises(Exception, match="read-only transaction"):
            Unit.objects.create(name="Probe unit", short_name="pr")
    finally:
        settings.DENSTOCK_MODE = "test"
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("RESET default_transaction_read_only")
            cursor.execute(f'DROP OWNED BY "{role}"')
            cursor.execute(f'DROP ROLE "{role}"')
    assert response.status_code == 302, response.content.decode()[:500]
    assert CustomerRequest.objects.get().lines.get().part_type_id == part.pk


def test_system_checks_need_no_migration_ledger(restricted_role):
    """`check --database default` runs as the public role without django_migrations."""
    from django.core.management import call_command

    _as(restricted_role)
    try:
        call_command("check", databases=["default"], verbosity=0)
    finally:
        _reset()


# --- Telegram rows: the public role may only attach them to its own request ------------------

TELEGRAM_REFUSED = "telegram row refused"


def _victim_request(public_catalog, key, messenger="max"):
    from apps.customer_requests.services import RequestLineInput, create_customer_request

    from .test_customer_requests import POLICY

    part = public_catalog.part("VICTIM PART", article=f"VIC-{key[:6]}")
    request, _created = create_customer_request(
        customer_name="Другой клиент",
        customer_phone="+7 (912) 765-43-21",
        preferred_messenger=messenger,
        comment="",
        lines=[RequestLineInput(part_id=part.pk, quantity="1", supply_inquiry=True)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=key,
    )
    return request


def _insert(sql, params, *, proof=None, refused):
    ctx = (
        pytest.raises(ProgrammingError, match=TELEGRAM_REFUSED)
        if refused
        else __import__("contextlib").nullcontext()
    )
    with ctx, transaction.atomic():
        with connection.cursor() as cursor:
            if proof is not None:
                cursor.execute(
                    "SELECT set_config('denstock.telegram_request_proof', %s, true)", [proof]
                )
            cursor.execute(sql, params)


def _linked_conversation(request_id):
    return (
        "INSERT INTO customer_requests_telegramconversation (public_id, request_id, status, "
        "customer_chat_id, customer_user_id, customer_username, created_at, updated_at) "
        "VALUES (%s, %s, 'linked', 666, 666, 'attacker', now(), now())",
        [str(uuid.uuid4()), request_id],
    )


def _waiting_conversation(request_id):
    return (
        "INSERT INTO customer_requests_telegramconversation (public_id, request_id, status, "
        "customer_username, created_at, updated_at) "
        "VALUES (%s, %s, 'awaiting_link', '', now(), now())",
        [str(uuid.uuid4()), request_id],
    )


def _token(request_id, token_hash):
    return (
        "INSERT INTO customer_requests_customerrequestmessengerlinktoken "
        "(request_id, channel, token_hash, created_at, expires_at) "
        "VALUES (%s, 'telegram', %s, now(), now() + interval '1 hour')",
        [request_id, token_hash],
    )


def _outbox(request_id, kind, dedupe_key):
    return (
        "INSERT INTO customer_requests_telegramoutboxevent "
        "(kind, request_id, dedupe_key, status, attempts, created_at, next_attempt_at) "
        "VALUES (%s, %s, %s, 'pending', 0, now(), now())",
        [kind, request_id, dedupe_key],
    )


def test_public_role_cannot_attach_telegram_rows_to_another_customers_request(
    restricted_role, public_catalog
):
    victim = _victim_request(public_catalog, "victim-key-" + "v" * 21)
    forged = [
        _linked_conversation(victim.pk),
        _waiting_conversation(victim.pk),
        _token(victim.pk, "a" * 64),
        _outbox(victim.pk, "new_request", f"new_request:{victim.pk}"),
    ]
    _as(restricted_role)
    try:
        for sql, params in forged:
            _insert(sql, params, refused=True)  # no proof at all
            _insert(sql, params, proof="attacker-own-key-" + "x" * 15, refused=True)
    finally:
        _reset()
    from apps.customer_requests.models import (
        CustomerRequestMessengerLinkToken,
        TelegramConversation,
        TelegramOutboxEvent,
    )

    assert not TelegramConversation.objects.filter(request=victim).exists()
    assert not CustomerRequestMessengerLinkToken.objects.filter(request=victim).exists()
    assert not TelegramOutboxEvent.objects.filter(request=victim).exists()


def test_even_with_its_own_proof_the_public_role_inserts_only_initial_shapes(
    restricted_role, public_catalog
):
    key = "own-request-key-" + "o" * 16
    own = _victim_request(public_catalog, key)
    _as(restricted_role)
    try:
        _insert(*_linked_conversation(own.pk), proof=key, refused=True)
        _insert(*_outbox(own.pk, "customer_message", "customer_message:1"), proof=key, refused=True)
        _insert(*_outbox(own.pk, "new_request", "new_request:999999"), proof=key, refused=True)
        _insert(
            "INSERT INTO customer_requests_customerrequestmessengerlinktoken "
            "(request_id, channel, token_hash, created_at, expires_at, used_at) "
            "VALUES (%s, 'telegram', %s, now(), now() + interval '1 hour', now())",
            [own.pk, "b" * 64],
            proof=key,
            refused=True,
        )
        _insert(
            "INSERT INTO customer_requests_customerrequestmessengerlinktoken "
            "(request_id, channel, token_hash, created_at, expires_at) "
            "VALUES (%s, 'telegram', %s, now(), now() + interval '30 days')",
            [own.pk, "c" * 64],
            proof=key,
            refused=True,
        )
        # The legitimate initial rows of its own request are accepted.
        _insert(*_waiting_conversation(own.pk), proof=key, refused=False)
        _insert(*_outbox(own.pk, "new_request", f"new_request:{own.pk}"), proof=key, refused=False)
        _insert(*_token(own.pk, "d" * 64), proof=key, refused=False)
    finally:
        _reset()


def test_internal_role_is_not_restricted_by_the_telegram_insert_guard(db, public_catalog):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL trigger")
    victim = _victim_request(public_catalog, "internal-key-" + "i" * 19)
    _insert(*_linked_conversation(victim.pk), refused=False)
    _insert(*_token(victim.pk, "e" * 64), refused=False)
