"""Freeze/export and conservative failback eligibility evaluation."""

from __future__ import annotations

import hmac
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import backup
from .emergency_environment import validate_database_target
from .emergency_manifest import validate_manifest
from .emergency_state import record_event
from .models import DeploymentState, OfflineSession
from .write_guard import acquire_failover_lock, lifecycle_write


class FailbackError(RuntimeError):
    pass


@dataclass
class FailbackDecision:
    status: str
    reasons: list[str] = field(default_factory=list)
    differences: dict = field(default_factory=dict)

    @property
    def eligible(self):
        return self.status == OfflineSession.Status.ELIGIBLE

    def as_dict(self):
        return {
            "status": self.status,
            "eligible": self.eligible,
            "reasons": self.reasons,
            "differences": self.differences,
            "automatic_production_overwrite": "disabled",
        }


def _freeze_for_export(*, resume=False) -> OfflineSession:
    validate_database_target(mode="emergency-local")
    with transaction.atomic(), lifecycle_write():
        acquire_failover_lock(exclusive=True)
        state = DeploymentState.objects.select_for_update().get(
            pk=DeploymentState.SINGLETON_PK
        )
        session = (
            OfflineSession.objects.select_for_update()
            .filter(
                status__in=[
                    OfflineSession.Status.ACTIVE,
                    OfflineSession.Status.FREEZING,
                    OfflineSession.Status.EXPORT_FAILED,
                ]
            )
            .first()
        )
        if session is None:
            raise FailbackError("Нет active или recoverable offline session для экспорта.")
        if session.status == OfflineSession.Status.FREEZING and not resume:
            raise FailbackError(
                "Экспорт уже выполняется или был прерван. Для восстановления укажите --resume."
            )
        if state.write_state not in {
            DeploymentState.WriteState.EMERGENCY_ACTIVE,
            DeploymentState.WriteState.EMERGENCY_FROZEN,
        }:
            raise FailbackError("Local deployment не находится в emergency mode.")
        session.status = OfflineSession.Status.FREEZING
        session.save(update_fields=["status", "updated_at"])
        state.write_state = DeploymentState.WriteState.EMERGENCY_FROZEN
        state.state_reason = f"offline-session-freezing:{session.id}"
        state.state_changed_at = timezone.now()
        state.save(update_fields=["write_state", "state_reason", "state_changed_at", "updated_at"])
        record_event(
            "offline_freeze",
            "resumed" if resume else "success",
            session=session,
        )
        return session


def freeze_and_export(*, actor="operator", root=None, resume=False) -> OfflineSession:
    session = _freeze_for_export(resume=resume)
    lineage = {
        "offline_lineage": {
            "offline_session_id": str(session.id),
            "base_backup_run_id": session.base_backup_run_id,
            "base_database_identity": session.base_manifest.get("database_identity"),
            "base_business_sha256": session.base_data_marker.get("business_sha256"),
            "base_media_sha256": session.base_media_sha256,
        }
    }
    try:
        run = backup.backup_all(
            root=root,
            trigger="emergency_final",
            extra_manifest=lineage,
        )
        report = validate_manifest(run, expected_source="emergency-local")
        if not report.ok:
            raise FailbackError("Final export verification failed: " + "; ".join(report.errors))
    except Exception as exc:
        with lifecycle_write():
            OfflineSession.objects.filter(pk=session.pk).update(
                status=OfflineSession.Status.EXPORT_FAILED,
                failback_report={"export_error": str(exc)[:500]},
            )
            record_event(
                "offline_export",
                "failed",
                session=session,
                actor=actor,
                details={"error": str(exc)},
            )
        if isinstance(exc, FailbackError):
            raise
        raise FailbackError(str(exc)) from exc

    with transaction.atomic(), lifecycle_write():
        locked = OfflineSession.objects.select_for_update().get(pk=session.pk)
        if locked.status != OfflineSession.Status.FREEZING:
            raise FailbackError("Offline session state changed during export.")
        locked.status = OfflineSession.Status.FROZEN
        locked.ended_at = timezone.now()
        locked.final_backup_run_id = run.name
        locked.final_manifest = report.manifest
        locked.final_data_marker = report.manifest["data_state"]
        locked.save(
            update_fields=[
                "status",
                "ended_at",
                "final_backup_run_id",
                "final_manifest",
                "final_data_marker",
                "updated_at",
            ]
        )
        record_event(
            "offline_export",
            "success",
            session=locked,
            actor=actor,
            details={"final_backup_run_id": locked.final_backup_run_id},
        )
        return locked


def fetch_production_probe(url: str, *, token: str, timeout=30) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise FailbackError("Production probe requires HTTPS.")
    if not token:
        raise FailbackError("DENSTOCK_EMERGENCY_PROBE_TOKEN не задан.")
    endpoint = url.rstrip("/") + "/operations/emergency/probe/"
    request = urllib.request.Request(
        endpoint,
        headers={"X-Denstock-Emergency-Probe": token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise FailbackError("Production probe недоступен или вернул некорректный ответ.") from exc
    if not isinstance(payload, dict):
        raise FailbackError("Production probe payload должен быть JSON object.")
    return payload


def probe_token_matches(candidate: str) -> bool:
    expected = settings.DENSTOCK_EMERGENCY_PROBE_TOKEN
    return bool(expected and hmac.compare_digest(str(candidate or ""), str(expected)))


def _table_differences(base: dict, current: dict) -> dict:
    differences = {}
    base_tables = base.get("tables", {})
    current_tables = current.get("tables", {})
    for table in sorted(set(base_tables) | set(current_tables)):
        if base_tables.get(table) != current_tables.get(table):
            differences[table] = {
                "base": base_tables.get(table),
                "production": current_tables.get(table),
            }
    return differences


def evaluate_failback(session: OfflineSession, production: dict, final_run: str | Path):
    report = validate_manifest(final_run, expected_source="emergency-local")
    reasons = []
    differences = {}
    blocked = False
    conflict = False
    if session.status not in {
        OfflineSession.Status.FROZEN,
        OfflineSession.Status.ELIGIBLE,
        OfflineSession.Status.CONFLICT,
        OfflineSession.Status.BLOCKED,
    }:
        blocked = True
        reasons.append("offline session ещё не заморожена")
    if not report.ok:
        blocked = True
        reasons.extend(f"local final backup: {error}" for error in report.errors)
        final = {}
    else:
        final = report.manifest

    base = session.base_manifest
    base_data = session.base_data_marker
    production_data = production.get("data_state") or {}
    if production.get("schema_version") != 1:
        blocked = True
        reasons.append("production probe schema version неизвестна")
    if production.get("mode") != "production":
        blocked = True
        reasons.append("production probe не подтверждает production mode")
    if production.get("write_state") != DeploymentState.WriteState.MAINTENANCE:
        blocked = True
        reasons.append("production не находится под maintenance write lock")
    if not production.get("stable_snapshot"):
        blocked = True
        reasons.append("production probe не подтвердил стабильный snapshot")
    if production_data.get("database_identity") != base.get("database_identity"):
        blocked = True
        reasons.append("production database identity не совпадает с common ancestor")
    if production.get("migration_fingerprint") != session.base_migration_fingerprint:
        blocked = True
        reasons.append("production migration state отличается от base")
    if production.get("app_commit") != session.base_app_commit:
        blocked = True
        reasons.append("production application commit отличается от base")
    if production_data.get("business_generation") != base_data.get("business_generation"):
        conflict = True
        reasons.append("production business generation изменилась после base")
        differences["business_generation"] = {
            "base": base_data.get("business_generation"),
            "production": production_data.get("business_generation"),
        }
    if production_data.get("business_sha256") != base_data.get("business_sha256"):
        conflict = True
        reasons.append("production business data fingerprint изменился после base")
        differences["business_sha256"] = {
            "base": base_data.get("business_sha256"),
            "production": production_data.get("business_sha256"),
        }
        differences["business_tables"] = _table_differences(base_data, production_data)
    if production.get("media_tree_sha256") != session.base_media_sha256:
        conflict = True
        reasons.append("production media изменились после base")
        differences["media_tree_sha256"] = {
            "base": session.base_media_sha256,
            "production": production.get("media_tree_sha256"),
        }

    if final:
        lineage = final.get("offline_lineage") or {}
        expected_lineage = {
            "offline_session_id": str(session.id),
            "base_backup_run_id": session.base_backup_run_id,
            "base_database_identity": base.get("database_identity"),
            "base_business_sha256": base_data.get("business_sha256"),
            "base_media_sha256": session.base_media_sha256,
        }
        if lineage != expected_lineage:
            blocked = True
            reasons.append("local final backup не подтверждает общий ancestor")
        if final.get("database_identity") != base.get("database_identity"):
            blocked = True
            reasons.append("local database identity отличается от base")
        if final.get("migration_fingerprint") != session.base_migration_fingerprint:
            blocked = True
            reasons.append("local migration state отличается от base")
        if final.get("app_commit") != session.base_app_commit:
            blocked = True
            reasons.append("local application commit отличается от base")
        if final.get("source_instance_id") != session.instance_id:
            blocked = True
            reasons.append("local final backup создан другим instance")

    status = (
        OfflineSession.Status.CONFLICT
        if conflict
        else OfflineSession.Status.BLOCKED
        if blocked
        else OfflineSession.Status.ELIGIBLE
    )
    decision = FailbackDecision(status=status, reasons=reasons, differences=differences)
    with lifecycle_write():
        OfflineSession.objects.filter(pk=session.pk).update(
            status=status, failback_report=decision.as_dict()
        )
        record_event(
            "failback_check",
            "success" if decision.eligible else status,
            session=session,
            details={"reasons": reasons, "difference_groups": sorted(differences)},
        )
    return decision
