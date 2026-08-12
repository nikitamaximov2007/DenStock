"""Start and inspect a controlled local offline session."""

from __future__ import annotations

import socket
from datetime import datetime

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from . import backup
from .emergency_environment import validate_database_target
from .emergency_manifest import validate_manifest
from .emergency_state import business_state_marker, migration_state, record_event
from .models import DeploymentState, OfflineSession
from .standby import EmergencyPaths, StandbyError, load_control
from .write_guard import acquire_failover_lock, lifecycle_write


class EmergencyLifecycleError(RuntimeError):
    pass


def _aware_datetime(value: str):
    parsed = datetime.fromisoformat(value)
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _active_standby(paths=None) -> tuple[dict, dict]:
    paths = paths or EmergencyPaths.configured()
    try:
        active = load_control(paths).get("active_standby")
    except StandbyError as exc:
        raise EmergencyLifecycleError(str(exc)) from exc
    if not active:
        raise EmergencyLifecycleError("Проверенной standby-копии нет.")
    report = validate_manifest(active["manifest_path"], expected_source="production")
    if not report.ok:
        raise EmergencyLifecycleError("Standby manifest invalid: " + "; ".join(report.errors))
    if connection.settings_dict.get("NAME") != active["database_name"]:
        raise EmergencyLifecycleError("Запущена не active standby database.")
    return active, report.manifest


@transaction.atomic
def start_offline_session(*, kind: str, actor=None, paths=None) -> OfflineSession:
    validate_database_target(mode="emergency-local")
    active, manifest = _active_standby(paths)
    acquire_failover_lock(exclusive=True)
    with lifecycle_write():
        state = DeploymentState.objects.select_for_update().get(
            pk=DeploymentState.SINGLETON_PK
        )
        if state.write_state not in {
            DeploymentState.WriteState.NORMAL,
            DeploymentState.WriteState.MAINTENANCE,
        }:
            raise EmergencyLifecycleError("Database уже находится в emergency lifecycle.")
        if OfflineSession.objects.filter(
            status__in=[
                OfflineSession.Status.ACTIVE,
                OfflineSession.Status.FREEZING,
                OfflineSession.Status.EXPORT_FAILED,
            ]
        ).exists():
            raise EmergencyLifecycleError("Offline session уже активна.")
        if kind == OfflineSession.Kind.PLANNED and manifest.get("consistency") != (
            "single_writer_locked"
        ):
            raise EmergencyLifecycleError(
                "Planned failover требует backup, созданный под production maintenance lock."
            )
        local_commit = settings.DENSTOCK_APP_COMMIT or backup._git_commit()
        if manifest["app_commit"] != local_commit:
            raise EmergencyLifecycleError("Application commit не совпадает с base backup.")
        current_migrations = migration_state()
        if current_migrations["fingerprint"] != manifest["migration_fingerprint"]:
            raise EmergencyLifecycleError("Migration state не совпадает с base backup.")
        marker = business_state_marker()
        if marker["business_sha256"] != manifest["data_state"]["business_sha256"]:
            raise EmergencyLifecycleError("Local standby data отличается от verified backup.")
        if marker["database_identity"] != manifest["database_identity"]:
            raise EmergencyLifecycleError("Database identity отличается от verified backup.")

        session = OfflineSession.objects.create(
            kind=kind,
            status=OfflineSession.Status.ACTIVE,
            local_hostname=socket.gethostname(),
            instance_id=settings.DENSTOCK_INSTANCE_ID,
            base_backup_run_id=manifest["backup_run_id"],
            base_backup_created_at=_aware_datetime(manifest["created_at"]),
            base_manifest=manifest,
            base_data_marker=manifest["data_state"],
            base_media_sha256=manifest["media_tree_sha256"],
            base_app_commit=manifest["app_commit"],
            base_migration_fingerprint=manifest["migration_fingerprint"],
            started_by=actor if getattr(actor, "pk", None) else None,
        )
        state.write_state = DeploymentState.WriteState.EMERGENCY_ACTIVE
        state.state_reason = f"offline-session:{session.id}"
        state.state_changed_at = timezone.now()
        state.save(update_fields=["write_state", "state_reason", "state_changed_at", "updated_at"])
        record_event(
            "offline_session_started",
            "success",
            session=session,
            actor=getattr(actor, "username", actor or "operator"),
            details={"kind": kind, "base_backup_run_id": manifest["backup_run_id"]},
        )
        return session


def emergency_context() -> dict:
    if settings.DENSTOCK_MODE != "emergency-local":
        return {"enabled": False}
    try:
        state = DeploymentState.get_solo()
        session = OfflineSession.objects.filter(
            status__in=[
                OfflineSession.Status.ACTIVE,
                OfflineSession.Status.FREEZING,
                OfflineSession.Status.EXPORT_FAILED,
                OfflineSession.Status.FROZEN,
                OfflineSession.Status.ELIGIBLE,
                OfflineSession.Status.CONFLICT,
                OfflineSession.Status.BLOCKED,
            ]
        ).first()
    except Exception:
        return {"enabled": True, "status": "unavailable"}
    return {
        "enabled": True,
        "status": state.write_state,
        "session": session,
        "instance_id": settings.DENSTOCK_INSTANCE_ID,
        "writable": state.write_state == DeploymentState.WriteState.EMERGENCY_ACTIVE,
    }
