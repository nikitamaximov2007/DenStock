"""Разворачивание базы с нуля не должно упираться в защиту записи.

Защита записи ставится перед каждой миграцией. Пока `operations.0002` не
создала управляющую таблицу, любая более ранняя миграция, которая пишет данные
или пересоздаёт таблицу, натыкается на её отсутствие. Это единственное
состояние, которое защите разрешено пропустить.

Раньше это состояние опознавалось по коду ошибки PostgreSQL. SQLite такого
кода не сообщает, поэтому развернуть базу с нуля в обычном режиме на нём было
нельзя: миграция падала на первой же таблице, которую SQLite пересоздаёт
целиком. Тесты идут в тестовом режиме, где защита отключена, и потому этого не
показывали.

Главная гарантия остаётся прежней: пропуск возможен только до применения
определяющей миграции. Потеря таблицы на уже развёрнутой базе обязана
приводить к отказу, а не к молчаливой записи.
"""
from __future__ import annotations

import pytest
from django.db.utils import OperationalError, ProgrammingError

from apps.operations.models import DeploymentState
from apps.operations.write_guard import _is_missing_deployment_state_table

TABLE = DeploymentState._meta.db_table


class _PostgresCause(Exception):
    sqlstate = "42P01"


class _SqliteCause(Exception):
    """У SQLite кода состояния нет вовсе."""


def _wrapped(cause: Exception, wrapper=OperationalError) -> Exception:
    error = wrapper(str(cause))
    error.__cause__ = cause
    return error


def test_postgresql_missing_table_is_recognised():
    cause = _PostgresCause(f'relation "{TABLE}" does not exist')
    assert _is_missing_deployment_state_table(_wrapped(cause, ProgrammingError)) is True


def test_sqlite_missing_table_is_recognised():
    """Без этого разворачивание с нуля на SQLite падало на первой миграции."""
    cause = _SqliteCause(f"no such table: {TABLE}")
    assert _is_missing_deployment_state_table(_wrapped(cause)) is True


def test_postgresql_stays_as_strict_as_before():
    """Там, где код состояния есть, требование к нему не ослабло."""
    cause = _PostgresCause(f'relation "{TABLE}" does not exist')
    cause.sqlstate = "42501"  # недостаточно прав, а не отсутствующая таблица
    assert _is_missing_deployment_state_table(_wrapped(cause)) is False


@pytest.mark.parametrize(
    "message",
    [
        "no such table: sales_sale",
        'relation "inventory_stocklot" does not exist',
        "database is locked",
    ],
)
def test_another_table_is_never_mistaken_for_the_control_table(message):
    cause = _SqliteCause(message)
    assert _is_missing_deployment_state_table(_wrapped(cause)) is False


def test_an_error_without_a_cause_is_not_bootstrap():
    assert _is_missing_deployment_state_table(OperationalError("no such table")) is False


def test_the_real_protection_is_the_unapplied_migration(db):
    """Пропуск возможен только до применения определяющей миграции.

    На развёрнутой базе она применена, поэтому даже точное сообщение об
    отсутствии управляющей таблицы не должно открывать запись.
    """
    from apps.operations.write_guard import _deployment_state_schema_is_migrated

    assert _deployment_state_schema_is_migrated(using="default") is True, (
        "определяющая миграция не применена: проверка бессмысленна"
    )
