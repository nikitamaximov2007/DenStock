"""Global write guard and PostgreSQL failover lock protocol."""

from __future__ import annotations

import contextvars
import re
from contextlib import contextmanager

from django.apps import apps
from django.conf import settings
from django.db import OperationalError, ProgrammingError, connections, transaction
from django.db.backends.signals import connection_created
from django.db.models import F
from django.http import HttpResponse

from .emergency_state import BUSINESS_APP_LABELS
from .models import DeploymentState

FAILOVER_ADVISORY_LOCK_ID = 0x44454E53544F434B
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
EXEMPT_WRITE_PATHS = frozenset({"/login/", "/logout/", "/operations/backups/create/"})
_internal = contextvars.ContextVar("denstock_write_guard_internal", default=False)
_table_pattern = None


class BusinessWriteBlocked(RuntimeError):
    pass


def _guarded_table_pattern():
    global _table_pattern
    if _table_pattern is None:
        tables = sorted(
            {
                model._meta.db_table
                for model in apps.get_models(include_auto_created=True)
                if model._meta.app_label in BUSINESS_APP_LABELS
            },
            key=len,
            reverse=True,
        )
        joined = "|".join(re.escape(table) for table in tables)
        _table_pattern = re.compile(rf'(?<![A-Za-z0-9_])"?(?:{joined})"?(?![A-Za-z0-9_])', re.I)
    return _table_pattern


def _is_business_mutation(sql_text) -> bool:
    sql_text = str(sql_text or "")
    upper = sql_text.lstrip().upper()
    mutating = upper.startswith(("INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"))
    if upper.startswith("WITH"):
        mutating = bool(re.search(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", upper))
    return bool(mutating and _guarded_table_pattern().search(sql_text))


def _read_write_state(using="default") -> str:
    try:
        return (
            DeploymentState.objects.using(using)
            .values_list("write_state", flat=True)
            .get(pk=DeploymentState.SINGLETON_PK)
        )
    except DeploymentState.DoesNotExist as exc:
        raise BusinessWriteBlocked("Deployment state is missing; writes fail closed.") from exc


def _state_allows_business_write(state: str) -> bool:
    mode = settings.DENSTOCK_MODE
    if mode == "emergency-local":
        return state == DeploymentState.WriteState.EMERGENCY_ACTIVE
    if mode == "production":
        return state == DeploymentState.WriteState.NORMAL
    return mode in {"development", "test"} and state == DeploymentState.WriteState.NORMAL


def assert_business_writes_allowed(*, using="default") -> None:
    state = _read_write_state(using)
    if not _state_allows_business_write(state):
        raise BusinessWriteBlocked(
            f"Business writes are blocked in {settings.DENSTOCK_MODE}/{state}."
        )


def execute_guard(execute, sql, params, many, context):
    if _internal.get() or not _is_business_mutation(sql):
        return execute(sql, params, many, context)
    if settings.DENSTOCK_MODE == "test":
        return execute(sql, params, many, context)
    alias = context["connection"].alias
    with _business_mutation_lock(using=alias):
        token = _internal.set(True)
        try:
            try:
                assert_business_writes_allowed(using=alias)
            except (OperationalError, ProgrammingError):
                database = context["connection"]
                if DeploymentState._meta.db_table in database.introspection.table_names():
                    raise
                return execute(sql, params, many, context)
            DeploymentState.objects.using(alias).filter(
                pk=DeploymentState.SINGLETON_PK
            ).update(business_generation=F("business_generation") + 1)
            return execute(sql, params, many, context)
        finally:
            _internal.reset(token)


def install_guard(sender=None, connection=None, **kwargs):
    database_connection = connection
    if database_connection is None:
        return
    if execute_guard not in database_connection.execute_wrappers:
        database_connection.execute_wrappers.append(execute_guard)


def install_all_guards() -> None:
    connection_created.connect(
        install_guard, dispatch_uid="denstock.operations.install_write_guard"
    )
    for database_connection in connections.all(initialized_only=True):
        install_guard(connection=database_connection)


def acquire_failover_lock(*, exclusive: bool, using="default") -> None:
    database = connections[using]
    if database.vendor != "postgresql":
        return
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    token = _internal.set(True)
    try:
        with database.cursor() as cursor:
            cursor.execute(f"SELECT {function}(%s)", [FAILOVER_ADVISORY_LOCK_ID])
    finally:
        _internal.reset(token)


@contextmanager
def _business_mutation_lock(*, using="default"):
    """Hold a transaction lock across the state check and mutation."""
    database = connections[using]
    if database.vendor != "postgresql":
        yield
        return
    with transaction.atomic(using=using):
        acquire_failover_lock(exclusive=False, using=using)
        yield


@contextmanager
def lifecycle_write():
    token = _internal.set(True)
    try:
        yield
    finally:
        _internal.reset(token)


class BusinessWriteGuardMiddleware:
    """Serialize freeze against complete unsafe HTTP transactions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in SAFE_METHODS:
            return self.get_response(request)
        if request.path in EXEMPT_WRITE_PATHS:
            with lifecycle_write():
                return self.get_response(request)
        try:
            with transaction.atomic():
                acquire_failover_lock(exclusive=False)
                assert_business_writes_allowed()
                return self.get_response(request)
        except BusinessWriteBlocked:
            return HttpResponse(
                "Запись временно запрещена: система работает только для чтения.",
                status=423,
                content_type="text/plain; charset=utf-8",
            )
