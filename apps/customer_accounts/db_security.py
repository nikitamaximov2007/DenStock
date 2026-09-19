"""Calls into the PostgreSQL functions of migration 0002. PostgreSQL only."""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection, transaction


def read_write() -> None:
    """The public role's transactions default to read-only; open this one for writing.

    Must be the first statement of the transaction; on the internal role it is
    a harmless no-op.
    """
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ WRITE")


def bind_session(session_hash: str) -> None:
    """Tell the views whose data this transaction may see."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('prostor.session_hash', %s, true)", [session_hash or ""]
            )


@contextmanager
def account_transaction(session_hash: str, *, write: bool = False):
    """One transaction bound to one session, read-only unless asked otherwise."""
    with transaction.atomic():
        if write:
            read_write()
        bind_session(session_hash)
        yield


def session_account_id(session_hash: str) -> int | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT customer_account_session_account(%s)", [session_hash])
        row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def logout_in_database(session_hash: str) -> None:
    with transaction.atomic():
        read_write()
        with connection.cursor() as cursor:
            cursor.execute("SELECT customer_account_logout(%s)", [session_hash])


def complete_attempt_in_database(
    *,
    browser_hash: str,
    code: str,
    new_session_hash: str,
    current_session_hash: str,
    session_days: int,
    max_tries: int,
) -> tuple[str, int | None]:
    with transaction.atomic():
        read_write()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT outcome, account_id FROM customer_account_complete_attempt"
                "(%s, %s, %s, %s, %s, %s)",
                [
                    browser_hash,
                    code,
                    new_session_hash,
                    current_session_hash,
                    session_days,
                    max_tries,
                ],
            )
            outcome, account_id = cursor.fetchone()
    return outcome, (int(account_id) if account_id is not None else None)
