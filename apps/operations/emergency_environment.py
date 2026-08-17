"""Fail-closed deployment/database identity checks."""

from __future__ import annotations

import ipaddress

from django.conf import settings
from django.core.checks import Error, register
from django.db import connection


class EmergencySafetyError(RuntimeError):
    pass


def _normalized(value) -> str:
    return str(value or "").strip().lower().rstrip(".")


def _host_matches(host: str, candidates) -> bool:
    host = _normalized(host)
    for candidate in candidates:
        candidate = _normalized(candidate)
        if not candidate:
            continue
        if host == candidate:
            return True
        try:
            if ipaddress.ip_address(host) == ipaddress.ip_address(candidate):
                return True
        except ValueError:
            pass
    return False


def validate_database_target(*, mode=None, database=None) -> None:
    mode = _normalized(mode or settings.DENSTOCK_MODE)
    database = database or connection.settings_dict
    host = _normalized(database.get("HOST"))
    name = _normalized(database.get("NAME"))
    prefix = _normalized(settings.DENSTOCK_EMERGENCY_DB_PREFIX)
    emergency_hosts = settings.DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS
    production_hosts = settings.DENSTOCK_PRODUCTION_DB_HOSTS

    if mode == "emergency-local":
        if "postgresql" not in _normalized(database.get("ENGINE")):
            raise EmergencySafetyError("Emergency local mode requires PostgreSQL.")
        if _host_matches(host, production_hosts):
            raise EmergencySafetyError("Emergency local mode points to a production DB host.")
        if not _host_matches(host, emergency_hosts):
            raise EmergencySafetyError("Emergency local DB host is not allowlisted.")
        if not prefix or not name.startswith(prefix):
            raise EmergencySafetyError("Emergency local database name has an unsafe prefix.")
    elif mode == "production":
        if "postgresql" not in _normalized(database.get("ENGINE")):
            raise EmergencySafetyError("Production mode requires PostgreSQL.")
        if prefix and name.startswith(prefix):
            raise EmergencySafetyError("Production points to an emergency local database.")
        if _host_matches(host, emergency_hosts):
            raise EmergencySafetyError("Production points to an emergency local DB host.")
        if not _host_matches(host, production_hosts):
            raise EmergencySafetyError("Production DB host is not allowlisted.")
    elif mode not in {"development", "test"}:
        raise EmergencySafetyError(f"Unknown DENSTOCK_MODE: {mode or '?'}")


def validate_emergency_role(*, role=None) -> None:
    """Only the designated LAN primary may ever become an emergency writer."""
    role = _normalized(role or settings.DENSTOCK_EMERGENCY_ROLE)
    if role not in {"primary", "secondary"}:
        raise EmergencySafetyError("Emergency workstation role must be primary or secondary.")


@register()
def deployment_database_check(app_configs, **kwargs):
    try:
        validate_database_target()
        if _normalized(settings.DENSTOCK_MODE) == "emergency-local":
            validate_emergency_role()
    except EmergencySafetyError as exc:
        return [Error(str(exc), id="operations.E001")]
    return []
