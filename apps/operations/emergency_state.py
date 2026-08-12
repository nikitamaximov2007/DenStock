"""Deterministic deployment markers used by backup and failback safety checks."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from django.apps import apps
from django.conf import settings
from django.db import connections
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

from .models import DeploymentState, EmergencyAuditEvent

BUSINESS_APP_LABELS = frozenset(
    {
        "accounts",
        "actions",
        "ai_support",
        "brp",
        "catalog",
        "core",
        "counting",
        "inventory",
        "polaris",
        "procurement",
        "receipts",
        "repairs",
        "returns",
        "sales",
        "stocktaking",
        "suppliers",
        "warehouse",
        "writeoffs",
    }
)
SENSITIVE_DETAIL_KEYS = frozenset(
    {"authorization", "credential", "database_url", "password", "secret", "token"}
)


def _json_value(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    return str(value)


def _canonical_bytes(value) -> bytes:
    return json.dumps(
        _json_value(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_tree_sha256(root: str | Path) -> str:
    """Hash relative paths and contents, independent of tar metadata."""
    root = Path(root)
    digest = hashlib.sha256()
    if not root.exists():
        digest.update(b"empty-media-tree\n")
        return digest.hexdigest()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    if not files:
        digest.update(b"empty-media-tree\n")
    return digest.hexdigest()


def migration_state(*, using="default") -> dict:
    rows = sorted(
        MigrationRecorder(connections[using]).migration_qs.values_list("app", "name")
    )
    payload = [[app, name] for app, name in rows]
    return {
        "fingerprint": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        "applied": payload,
    }


def application_migration_state(*, using="default") -> dict:
    loader = MigrationLoader(connections[using], ignore_no_migrations=True)
    payload = [[app, name] for app, name in sorted(loader.disk_migrations)]
    return {
        "fingerprint": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        "available": payload,
    }


def business_state_marker(*, using="default") -> dict:
    """Hash all business rows and expose high-level per-table counters.

    The operation is intentionally expensive and is used only for backup/failback
    control. It avoids relying on row counts or commit SHA alone.
    """
    table_markers = {}
    combined = hashlib.sha256()
    models = sorted(
        (
            model
            for model in apps.get_models(include_auto_created=False)
            if model._meta.app_label in BUSINESS_APP_LABELS and model._meta.managed
        ),
        key=lambda model: model._meta.label_lower,
    )
    for model in models:
        fields = [field.attname for field in model._meta.concrete_fields]
        pk_name = model._meta.pk.attname
        digest = hashlib.sha256()
        count = 0
        max_pk = None
        queryset = model._default_manager.using(using).order_by(pk_name).values_list(*fields)
        for row in queryset.iterator(chunk_size=1000):
            digest.update(_canonical_bytes(row))
            digest.update(b"\n")
            count += 1
            pk_value = row[fields.index(pk_name)]
            max_pk = _json_value(pk_value)
        label = model._meta.label_lower
        marker = {"count": count, "max_pk": max_pk, "sha256": digest.hexdigest()}
        table_markers[label] = marker
        combined.update(_canonical_bytes([label, marker]))
        combined.update(b"\n")
    state = DeploymentState.objects.using(using).get(pk=DeploymentState.SINGLETON_PK)
    return {
        "database_identity": str(state.database_identity),
        "business_generation": state.business_generation,
        "business_sha256": combined.hexdigest(),
        "tables": table_markers,
    }


def sanitize_audit_details(details: dict | None) -> dict:
    clean = {}
    for key, value in (details or {}).items():
        lowered = str(key).lower()
        clean[str(key)] = "[REDACTED]" if lowered in SENSITIVE_DETAIL_KEYS else _json_value(value)
    return clean


def record_event(event_type, outcome, *, session=None, actor="", details=None, using="default"):
    return EmergencyAuditEvent.objects.using(using).create(
        event_type=event_type,
        outcome=outcome,
        session=session,
        actor=str(actor or "")[:150],
        details=sanitize_audit_details(details),
    )


def runtime_identity() -> dict:
    return {
        "mode": settings.DENSTOCK_MODE,
        "instance_id": settings.DENSTOCK_INSTANCE_ID,
        "app_commit": settings.DENSTOCK_APP_COMMIT,
    }
