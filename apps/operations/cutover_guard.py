"""Fail-closed runtime guard for the offline DR cutover.

The cutover (``offline_cutover``) stamps a generation marker into the
PostgreSQL comment of every database it touches (``COMMENT ON DATABASE``).
The comment travels with the database through ``ALTER DATABASE ... RENAME``,
commits in the same transaction as the rename, and is not part of ``pg_dump``
output without ``--create``, so it never leaks into backups.

Every new PostgreSQL connection of every DenisStock process (web, catalog-web,
both bots, ``migrate``, shells) reads that comment of the database it reached:

* no comment, or a comment that is not a DenisStock DR marker: normal work;
* a marker of an unfinished cutover (any state but ``verified`` or
  ``committed``), the old generation, or a rolled back slot: the connection is
  closed and ``GenerationRefused`` is raised.  Nothing is served from it;
* ``verified``/``committed`` new generation: the inode of MEDIA_ROOT and
  PRIVATE_MEDIA_ROOT must be the inode recorded for that generation, so the
  process cannot run the new database with old (or foreign) file trees.  The
  public catalog has no file stores and checks the database only.

The DR tools themselves open their connections inside ``guard_bypass()``.
SQLite (development and tests) is never guarded: DR works on PostgreSQL only.
"""

from __future__ import annotations

import contextvars
import json
import os
from contextlib import contextmanager

from django.conf import settings
from django.core.checks import Error, Tags, register
from django.db import DatabaseError, connections

MARKER_KEY = "denstock_dr"
MARKER_VERSION = 1
SERVING_STATES = frozenset({"verified", "committed"})
FILE_SLOTS = (("media", "MEDIA_ROOT"), ("private_media", "PRIVATE_MEDIA_ROOT"))
MARKER_SQL = (
    "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
    "WHERE datname = current_database()"
)

_bypass = contextvars.ContextVar("denstock_dr_guard_bypass", default=False)


class GenerationRefused(RuntimeError):
    pass


@contextmanager
def guard_bypass():
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)


def parse_marker(raw) -> dict | None:
    """A DenisStock DR marker, or None for no/foreign comment.

    A comment that claims to be ours but cannot be read is returned as an
    ``invalid`` marker: the guard refuses it instead of ignoring it.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return {"invalid": True} if MARKER_KEY in str(raw) else None
    if not isinstance(data, dict) or MARKER_KEY not in data:
        return None
    return data


def read_marker(database_connection) -> dict | None:
    with database_connection.cursor() as cursor:
        cursor.execute(MARKER_SQL)
        row = cursor.fetchone()
    return parse_marker(row[0] if row else None)


def _inode(path) -> int | None:
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def marker_problems(marker: dict | None, *, mode: str | None = None, roots=None) -> list[str]:
    """Why a process must not serve from a database carrying ``marker``."""
    if marker is None:
        return []
    if marker.get("invalid") or marker.get(MARKER_KEY) != MARKER_VERSION:
        return ["Метка DR-переключения в комментарии базы не читается: запуск запрещён."]
    cutover = marker.get("cutover_id", "?")
    role, state = marker.get("role"), marker.get("state")
    if role != "new" or state not in SERVING_STATES:
        return [
            f"База участвует в незавершённом DR-переключении {cutover} "
            f"(роль {role}, состояние {state}): запуск запрещён. "
            "Выполните dr_cutover_status и dr_cutover_recover."
        ]
    mode = str(mode or settings.DENSTOCK_MODE).strip().lower()
    if mode == "public-catalog":
        return []  # the public runtime has no file stores
    if roots is None:
        roots = {slot: getattr(settings, name) for slot, name in FILE_SLOTS}
    problems = []
    for slot, _setting in FILE_SLOTS:
        expected = marker.get(f"{slot}_ino")
        actual = _inode(roots[slot])
        if expected is None or actual != expected:
            problems.append(
                f"Каталог {slot} ({roots[slot]}) не принадлежит поколению "
                f"{marker.get('generation', '?')} переключения {cutover}: запуск запрещён."
            )
    return problems


def on_connection_created(sender=None, connection=None, **kwargs):
    if connection is None or connection.vendor != "postgresql" or _bypass.get():
        return
    problems = marker_problems(read_marker(connection))
    if problems:
        # Close before refusing: a persistent connection must not survive to
        # serve the next request without being checked again.
        connection.close()
        raise GenerationRefused(" ".join(problems))


def install_generation_guard() -> None:
    from django.db.backends.signals import connection_created

    connection_created.connect(
        on_connection_created, dispatch_uid="denstock.operations.dr_generation_guard"
    )


def runtime_problems(using: str = "default") -> list[str]:
    """Guard verdict for this process's settings, read without the guard."""
    database = connections[using]
    if database.vendor != "postgresql":
        return []
    with guard_bypass():
        try:
            marker = read_marker(database)
        finally:
            # Opened without the guard: never hand it on to other code.  Inside
            # an atomic block (tests) the connection was opened before, guarded.
            if not database.in_atomic_block:
                database.close()
    return marker_problems(marker)


def assert_startup_allowed(using: str = "default") -> None:
    """Called by the WSGI entry point: refuse to boot on a refused generation.

    An unreachable database is left to the normal startup failure; every
    later connection is still checked by ``on_connection_created``.
    """
    try:
        problems = runtime_problems(using)
    except DatabaseError:
        return
    if problems:
        raise GenerationRefused(" ".join(problems))


@register(Tags.database)
def dr_generation_check(app_configs, databases=None, **kwargs):
    errors = []
    for alias in databases or ():
        try:
            problems = runtime_problems(alias)
        except DatabaseError:
            continue
        errors.extend(Error(problem, id="operations.E010") for problem in problems)
    return errors
