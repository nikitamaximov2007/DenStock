"""The MAX schema migrations: additive, reversible, and independent of Telegram's rows.

Runs on SQLite and on PostgreSQL. On PostgreSQL it also proves the public
insert guard admits a ``max`` link after 0009 and refuses it again after a
rollback to 0008.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MAX_TABLES = {
    "customer_requests_maxconversation",
    "customer_requests_maxcustomerchat",
    "customer_requests_maxmessage",
    "customer_requests_maxoutboxevent",
    "customer_requests_maxoperatordelivery",
    "operations_maxbotruntime",
}
BEFORE = [
    ("customer_requests", "0007_telegram_link_attempt_cap"),
    ("operations", "0005_telegram_messaging"),
]
AFTER_SCHEMA = [("customer_requests", "0008_max_messaging"), ("operations", "0006_max_messaging")]
LATEST = [
    # Restore the current customer-request schema after exercising the
    # historical MAX rollback.  Newer realtime/attachment migrations are
    # additive and must be present before the serialized fixture is restored.
    ("customer_requests", "0012_maxmessage_attachment_and_more"),
    ("operations", "0006_max_messaging"),
]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


def _tables():
    return set(connection.introspection.table_names())


def _guard_source():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE proname = 'denstock_telegram_public_insert_guard'"
        )
        row = cursor.fetchone()
    return row[0] if row else ""


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_max_migrations_roll_back_and_forward_cleanly():
    try:
        assert MAX_TABLES <= _tables()
        if connection.vendor == "postgresql":
            assert "NOT IN ('telegram', 'max')" in _guard_source()

        _migrate(AFTER_SCHEMA)
        if connection.vendor == "postgresql":
            assert "NOT IN ('telegram', 'max')" not in _guard_source()
            assert "NEW.channel <> 'telegram'" in _guard_source()

        _migrate(BEFORE)
        assert not (MAX_TABLES & _tables())
        assert "customer_requests_telegrammessage" in _tables()
    finally:
        _migrate(LATEST)
    assert MAX_TABLES <= _tables()


def test_the_max_migrations_depend_only_on_what_they_extend():
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    assert loader.detect_conflicts() == {}
    schema = loader.graph.nodes[("customer_requests", "0008_max_messaging")]
    guard = loader.graph.nodes[("customer_requests", "0009_max_public_link_guard")]
    runtime = loader.graph.nodes[("operations", "0006_max_messaging")]
    from django.conf import settings

    user_app = settings.AUTH_USER_MODEL.split(".")[0]
    assert {tuple(dep) for dep in schema.dependencies if dep[0] != user_app} == {
        ("customer_requests", "0007_telegram_link_attempt_cap"),
    }
    assert guard.dependencies == [("customer_requests", "0008_max_messaging")]
    assert runtime.dependencies == [("operations", "0005_telegram_messaging")]
