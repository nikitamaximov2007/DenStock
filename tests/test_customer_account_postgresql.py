"""PostgreSQL: the database enforces the account rules, not just Python.

Two things are proved here.

**Parity** — ``complete_attempt``, ``session_account`` and ``revoke_session``
run the SECURITY DEFINER functions of migration 0002 on PostgreSQL and the
Python bodies of ``services`` on SQLite. The same scenarios must reach the same
outcome through both, or the SQLite suite would be proving something the
deployed runtime does not do.

**Least privilege** — the restricted public role is created inside the test
transaction from the real deployment script and the connection switches to it,
so every read and write below runs with the production grant set. A compromised
public process must not be able to mint a session, read another account's
requests or purchases, or see a cost, margin or supplier column.
"""

import uuid
from decimal import Decimal

import pytest
from django.conf import settings
from django.db import connection, transaction
from django.db.utils import ProgrammingError

from apps.customer_accounts import history, services, tokens
from apps.customer_accounts.db_security import account_transaction, bind_session
from apps.customer_accounts.models import (
    CustomerAccount,
    CustomerIdentity,
    CustomerLoginAttempt,
    CustomerSession,
    Provider,
)
from tests.customer_account_support import (
    link_customer_card,
    link_max_conversation,
    make_customer,
    make_request,
    make_sale,
    public_account_runtime,
    sign_in,
)

pytestmark = pytest.mark.postgresql

ROLE_SCRIPT = settings.BASE_DIR / "scripts" / "operations" / "create_public_catalog_role.sql"
ALICE_MAX = 8800001
BOB_MAX = 8800002


@pytest.fixture
def restricted_role(db):
    if connection.vendor != "postgresql":
        pytest.skip("The account's database guarantees need PostgreSQL")
    role = f"public_test_{uuid.uuid4().hex[:10]}"
    body = ROLE_SCRIPT.read_text()
    block = body[body.index("DO $$"):]
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE ROLE "{role}" NOLOGIN')
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


def _skip_unless_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("The account's database guarantees need PostgreSQL")


@pytest.fixture
def two_accounts(public_catalog):
    """Alice and Bob: one signed-in session, one owned request, one purchase each."""
    _skip_unless_postgresql()
    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    lot = public_catalog.stock(part, "20")
    data = {"part": part, "lot": lot, "catalog": public_catalog}
    with public_account_runtime():
        for who, user_id, price in (("alice", ALICE_MAX, "1000"), ("bob", BOB_MAX, "2000")):
            token = sign_in(user_id, name=who)
            account = CustomerAccount.objects.get(identities__provider_user_id=user_id)
            request = make_request(public_catalog, part, name=who, key=f"pg-{who}")
            link_max_conversation(request, user_id)
            services.claim_proven_requests(account, Provider.MAX, user_id)
            customer = make_customer(f"{who}-карточка")
            link_customer_card(account, customer, public_catalog.user)
            sale = make_sale(customer, part, lot=lot, unit_price=price)
            data[who] = {
                "token": token, "account": account, "request": request,
                "sale": sale, "customer": customer,
            }
        yield data


# --- Migrations -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_account_functions_and_views_exist_after_migration(db):
    _skip_unless_postgresql()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proname FROM pg_proc WHERE proname LIKE 'customer_account%%' ORDER BY 1"
        )
        functions = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            "SELECT viewname FROM pg_views WHERE viewname LIKE 'customer_account%%' ORDER BY 1"
        )
        views = [row[0] for row in cursor.fetchall()]
    assert set(functions) >= {
        "customer_account_claim",
        "customer_account_complete_attempt",
        "customer_account_current",
        "customer_account_current_customer",
        "customer_account_logout",
        "customer_account_session_account",
    }
    assert views == [
        "customer_account_request_lines",
        "customer_account_requests",
        "customer_account_sale_lines",
        "customer_account_sales",
    ]


@pytest.mark.django_db
def test_the_account_functions_are_revoked_from_public(db):
    _skip_unless_postgresql()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proname FROM pg_proc "
            "WHERE proname LIKE 'customer_account%%' "
            "AND has_function_privilege('public', oid, 'EXECUTE')"
        )
        assert cursor.fetchall() == []


@pytest.mark.django_db
def test_the_request_ownership_column_is_nullable_and_additive(db):
    """Old production rows stay valid: the column is NULL by default."""
    _skip_unless_postgresql()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'customer_requests_customerrequest' "
            "AND column_name = 'customer_account_id'"
        )
        is_nullable, default = cursor.fetchone()
    assert is_nullable == "YES" and default is None


# --- Parity with the Python rules -------------------------------------------------------------


@pytest.mark.django_db
def test_the_database_login_reaches_the_same_result_as_the_python_one(public_catalog):
    _skip_unless_postgresql()
    with public_account_runtime():
        token = sign_in(ALICE_MAX, name="Алиса")
        assert connection.vendor == "postgresql"
        account = CustomerAccount.objects.get()
        assert CustomerIdentity.objects.get().provider_user_id == ALICE_MAX
        assert services.session_account(token).pk == account.pk
        assert account.last_login_at is not None


@pytest.mark.django_db
def test_the_database_refuses_a_replay_an_expired_attempt_and_a_wrong_code(public_catalog):
    from datetime import timedelta

    from django.utils import timezone

    _skip_unless_postgresql()
    with public_account_runtime():
        attempt = services.create_attempt(
            purpose=CustomerLoginAttempt.Purpose.LOGIN,
            provider=Provider.MAX,
            client_key="pg",
        )
        reply = services.provider_confirmed(
            provider=Provider.MAX, token=attempt.token,
            provider_user_id=ALICE_MAX, chat_id=1, display_name="Алиса",
        )
        wrong = "000000" if reply.code != "000000" else "111111"
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=wrong
        ).outcome == services.Outcome.WRONG_CODE
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        ).ok
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        ).outcome == services.Outcome.INVALID

        stale = services.create_attempt(
            purpose=CustomerLoginAttempt.Purpose.LOGIN,
            provider=Provider.MAX,
            client_key="pg2",
        )
        stale_reply = services.provider_confirmed(
            provider=Provider.MAX, token=stale.token,
            provider_user_id=ALICE_MAX + 1, chat_id=1,
        )
        CustomerLoginAttempt.objects.filter(
            browser_hash=tokens.digest(stale.browser_secret)
        ).update(expires_at=timezone.now() - timedelta(seconds=1))
        assert services.complete_attempt(
            browser_secret=stale.browser_secret, code=stale_reply.code
        ).outcome == services.Outcome.EXPIRED


@pytest.mark.django_db
def test_the_database_logout_revokes_only_that_session(public_catalog):
    _skip_unless_postgresql()
    with public_account_runtime():
        phone = sign_in(ALICE_MAX)
        laptop = sign_in(ALICE_MAX)
        services.revoke_session(phone)
        assert services.session_account(phone) is None
        assert services.session_account(laptop) is not None


@pytest.mark.django_db
def test_the_database_claims_only_a_provably_owned_request(public_catalog):
    _skip_unless_postgresql()
    part = public_catalog.part("SPARK PLUG", article="SP-1", price="500")
    public_catalog.stock(part, "10")
    with public_account_runtime():
        mine = make_request(public_catalog, part, name="Алиса", key="pg-mine")
        theirs = make_request(public_catalog, part, name="Алиса", key="pg-theirs")
        link_max_conversation(mine, ALICE_MAX)
        link_max_conversation(theirs, BOB_MAX)
        sign_in(ALICE_MAX, name="Алиса")

        mine.refresh_from_db()
        theirs.refresh_from_db()
        account = CustomerAccount.objects.get(identities__provider_user_id=ALICE_MAX)
        assert mine.customer_account_id == account.pk
        assert theirs.customer_account_id is None


@pytest.mark.django_db
def test_the_views_and_the_python_reader_agree(two_accounts):
    """The PostgreSQL views must return exactly what SQLite's filters would."""
    with public_account_runtime():
        alice = two_accounts["alice"]
        with account_transaction(tokens.digest(alice["token"])):
            requests = history.account_requests(alice["account"])
            purchases = history.account_purchases(alice["account"])
        assert [r.id for r in requests] == [alice["request"].pk]
        assert [p.number for p in purchases] == [alice["sale"].number]
        assert purchases[0].lines[0].unit_price == Decimal("1000")


@pytest.mark.django_db
def test_an_unbound_transaction_sees_nothing_at_all(two_accounts):
    """No session digest bound means no account: the views fail closed."""
    with public_account_runtime():
        with account_transaction(""):
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM customer_account_requests")
                assert cursor.fetchone()[0] == 0
                cursor.execute("SELECT count(*) FROM customer_account_sales")
                assert cursor.fetchone()[0] == 0


@pytest.mark.django_db
def test_a_bound_session_sees_only_its_own_rows_through_the_views(two_accounts):
    with public_account_runtime():
        for who, other in (("alice", "bob"), ("bob", "alice")):
            with account_transaction(tokens.digest(two_accounts[who]["token"])):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id FROM customer_account_requests")
                    assert [row[0] for row in cursor.fetchall()] == [
                        two_accounts[who]["request"].pk
                    ]
                    cursor.execute("SELECT number FROM customer_account_sales")
                    assert [row[0] for row in cursor.fetchall()] == [
                        two_accounts[who]["sale"].number
                    ]
                    cursor.execute(
                        "SELECT count(*) FROM customer_account_sales WHERE number = %s",
                        [two_accounts[other]["sale"].number],
                    )
                    assert cursor.fetchone()[0] == 0


@pytest.mark.django_db
def test_a_revoked_session_stops_resolving_in_the_database(two_accounts):
    with public_account_runtime():
        token = two_accounts["alice"]["token"]
        services.revoke_session(token)
        with account_transaction(tokens.digest(token)):
            with connection.cursor() as cursor:
                cursor.execute("SELECT customer_account_current()")
                assert cursor.fetchone()[0] is None
                cursor.execute("SELECT count(*) FROM customer_account_requests")
                assert cursor.fetchone()[0] == 0


@pytest.mark.django_db
def test_a_deactivated_account_resolves_to_nobody_in_the_database(two_accounts):
    with public_account_runtime():
        alice = two_accounts["alice"]
        services.deactivate_account(alice["account"])
        with account_transaction(tokens.digest(alice["token"])):
            with connection.cursor() as cursor:
                cursor.execute("SELECT customer_account_current()")
                assert cursor.fetchone()[0] is None


# --- The restricted role ----------------------------------------------------------------------


@pytest.mark.django_db
def test_the_role_cannot_read_sessions_or_attempt_secrets_at_all(
    restricted_role, two_accounts
):
    """Session tokens and login codes are not readable, with or without a session."""
    _as(restricted_role)
    _refused("SELECT token_hash FROM customer_accounts_customersession")
    _refused("SELECT account_id FROM customer_accounts_customersession")
    _refused("SELECT code_hash FROM customer_accounts_customerloginattempt")
    _refused("SELECT browser_hash FROM customer_accounts_customerloginattempt")
    _refused("SELECT provider_user_id FROM customer_accounts_customerloginattempt")
    _reset()


@pytest.mark.django_db
def test_the_role_reads_identities_only_for_the_bound_session(restricted_role, two_accounts):
    """Identities ARE readable — that is how «MAX ✓ / Telegram ✓» is drawn — but
    row-level security narrows them to the session's own account, and an
    unbound transaction sees none at all."""
    alice, bob = two_accounts["alice"], two_accounts["bob"]
    _as(restricted_role)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM customer_accounts_customeridentity")
        assert cursor.fetchone()[0] == 0  # nothing bound: nothing visible

        bind_session(tokens.digest(alice["token"]))
        cursor.execute("SELECT account_id FROM customer_accounts_customeridentity")
        assert [row[0] for row in cursor.fetchall()] == [alice["account"].pk]
        cursor.execute(
            "SELECT count(*) FROM customer_accounts_customeridentity WHERE account_id = %s",
            [bob["account"].pk],
        )
        assert cursor.fetchone()[0] == 0
    _reset()


@pytest.mark.django_db
def test_the_role_cannot_mint_a_session_or_forge_an_identity(restricted_role, two_accounts):
    alice = two_accounts["alice"]
    _as(restricted_role)
    _refused(
        "INSERT INTO customer_accounts_customersession "
        "(account_id, token_hash, created_at, expires_at) "
        "VALUES (%s, 'forged', now(), now() + interval '30 days')",
        [alice["account"].pk],
    )
    _refused(
        "INSERT INTO customer_accounts_customeridentity "
        "(account_id, provider, provider_user_id, display_name, verified_at, created_at) "
        "VALUES (%s, 'max', 999999, '', now(), now())",
        [alice["account"].pk],
    )
    _reset()


@pytest.mark.django_db
def test_the_role_cannot_link_a_denisstock_customer_card(restricted_role, two_accounts):
    alice = two_accounts["alice"]
    _as(restricted_role)
    _refused(
        "INSERT INTO customer_accounts_customeraccountcustomerlink "
        "(account_id, customer_id, linked_at, linked_by_id) VALUES (%s, %s, now(), NULL)",
        [alice["account"].pk, two_accounts["bob"]["customer"].pk],
    )
    _reset()


@pytest.mark.django_db
def test_the_role_cannot_read_sales_or_requests_outside_the_account_views(
    restricted_role, two_accounts
):
    _as(restricted_role)
    _refused("SELECT cost_total, profit_total FROM sales_sale")
    _refused("SELECT unit_cost_rub, total_cost_rub, profit_rub FROM sales_saleline")
    _reset()


@pytest.mark.django_db
def test_the_account_views_expose_no_internal_money_column(restricted_role, two_accounts):
    _as(restricted_role)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name IN ('customer_account_sales', 'customer_account_sale_lines')"
        )
        columns = {row[0] for row in cursor.fetchall()}
    assert columns == {
        "id", "number", "sold_at", "sale_id", "part_type_id",
        "quantity", "unit_price", "total_price",
    }
    for forbidden in ["cost_total", "profit_total", "revenue_total", "unit_cost_rub",
                      "total_cost_rub", "profit_rub", "customer_id", "sold_by_id"]:
        assert forbidden not in columns, forbidden
    _reset()


@pytest.mark.django_db
def test_the_role_reads_only_its_own_account_row_under_row_level_security(
    restricted_role, two_accounts
):
    alice, bob = two_accounts["alice"], two_accounts["bob"]
    _as(restricted_role)
    bind_session(tokens.digest(alice["token"]))
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM customer_accounts_customeraccount")
        assert [row[0] for row in cursor.fetchall()] == [alice["account"].pk]
        cursor.execute(
            "SELECT count(*) FROM customer_accounts_customeraccount WHERE id = %s",
            [bob["account"].pk],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT id FROM customer_account_requests")
        assert [row[0] for row in cursor.fetchall()] == [alice["request"].pk]
    _reset()


@pytest.mark.django_db
def test_the_role_completes_a_login_only_through_the_checked_function(
    restricted_role, public_catalog
):
    """The one privileged step it has: and it still needs the messenger's code."""
    with public_account_runtime():
        attempt = services.create_attempt(
            purpose=CustomerLoginAttempt.Purpose.LOGIN,
            provider=Provider.MAX,
            client_key="pg-role",
        )
        reply = services.provider_confirmed(
            provider=Provider.MAX, token=attempt.token,
            provider_user_id=ALICE_MAX, chat_id=1, display_name="Алиса",
        )
        _as(restricted_role)
        wrong = "000000" if reply.code != "000000" else "111111"
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=wrong
        ).outcome == services.Outcome.WRONG_CODE
        completion = services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        )
        assert completion.ok
        _reset()
        assert CustomerSession.objects.filter(
            token_hash=tokens.digest(completion.session_token)
        ).count() == 1
