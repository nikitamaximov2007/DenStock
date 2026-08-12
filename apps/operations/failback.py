"""Freeze/export and conservative failback eligibility evaluation."""

from __future__ import annotations

import hmac
import json
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import backup
from .emergency_environment import validate_database_target
from .emergency_manifest import SHA256_RE, validate_manifest
from .emergency_state import (
    business_state_marker,
    media_tree_sha256,
    migration_state,
    record_event,
    sha256_file,
)
from .models import DeploymentState, OfflineSession
from .standby import (
    EmergencyPaths,
    control_lock,
    load_control,
    save_control,
    set_offline_lifecycle,
)
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
        state = DeploymentState.objects.select_for_update().get(pk=DeploymentState.SINGLETON_PK)
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


def freeze_and_export(*, actor="operator", root=None, resume=False, paths=None) -> OfflineSession:
    paths = paths or EmergencyPaths.configured()
    with control_lock(paths):
        return _freeze_and_export_locked(actor=actor, root=root, resume=resume, paths=paths)


def _freeze_and_export_locked(*, actor, root, resume, paths) -> OfflineSession:
    session = _freeze_for_export(resume=resume)
    set_offline_lifecycle(session, status=OfflineSession.Status.FREEZING, paths=paths)
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
        set_offline_lifecycle(
            session,
            status=OfflineSession.Status.EXPORT_FAILED,
            paths=paths,
            details={"recoverable": True},
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
        set_offline_lifecycle(
            locked,
            paths=paths,
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


def configured_production_url(candidate=None) -> str:
    """Return the pinned URL or fail before a probe token can leave the process."""
    configured = settings.DENSTOCK_PRODUCTION_URL.rstrip("/")
    if not configured:
        raise FailbackError("DENSTOCK_PRODUCTION_URL не задан.")
    selected = (candidate or configured).rstrip("/")
    if selected != configured:
        raise FailbackError(
            "Production URL отличается от DENSTOCK_PRODUCTION_URL; token не отправлен."
        )
    return selected


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


def prepare_failback_package(*, session=None, root=None, paths=None):
    """Create a verified handoff package. It never connects to or writes production."""
    validate_database_target(mode="emergency-local")
    paths = paths or EmergencyPaths.configured()
    session = (
        session
        or OfflineSession.objects.filter(
            status__in=[
                OfflineSession.Status.ELIGIBLE,
                OfflineSession.Status.CONFLICT,
                OfflineSession.Status.BLOCKED,
            ]
        ).first()
    )
    if session is None or not session.final_backup_run_id:
        raise FailbackError("Сначала завершите export и выполните failback-check.")
    backup_base = Path(root) if root else backup.backup_root()
    backup_base = backup_base.resolve()
    final_run = (backup_base / session.final_backup_run_id).resolve()
    if final_run.parent != backup_base or not final_run.is_dir():
        raise FailbackError("Final backup path находится вне BACKUP_ROOT.")
    report = validate_manifest(final_run, expected_source="emergency-local")
    if not report.ok:
        raise FailbackError("Final backup invalid: " + "; ".join(report.errors))

    manifest = report.manifest
    payload_names = ["manifest.json", manifest["database_dump_filename"]]
    if manifest.get("media_filename"):
        payload_names.append(manifest["media_filename"])
    package_metadata = {
        "schema_version": 1,
        "created_at": timezone.now().isoformat(),
        "offline_session_id": str(session.id),
        "failback_status": session.status,
        "automatic_production_overwrite": "disabled",
        "base_backup_run_id": session.base_backup_run_id,
        "final_backup_run_id": session.final_backup_run_id,
        "database_identity": manifest["database_identity"],
        "app_commit": manifest["app_commit"],
        "migration_fingerprint": manifest["migration_fingerprint"],
        "failback_report": session.failback_report,
    }
    packages = paths.root / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    package = packages / f"failback-{session.id}.zip"
    temporary = package.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in payload_names:
            archive.write(final_run / name, arcname=f"backup/{name}")
        archive.writestr(
            "failback-report.json",
            json.dumps(package_metadata, ensure_ascii=False, indent=2, sort_keys=True),
        )
    with zipfile.ZipFile(temporary, "r") as archive:
        broken = archive.testzip()
        if broken:
            temporary.unlink(missing_ok=True)
            raise FailbackError(f"Failback package повреждён: {broken}")
    temporary.replace(package)
    package_sha256 = sha256_file(package)
    checksum = package.with_suffix(package.suffix + ".sha256")
    checksum_temporary = checksum.with_suffix(checksum.suffix + ".tmp")
    checksum_temporary.write_text(f"{package_sha256}  {package.name}\n", encoding="ascii")
    checksum_temporary.replace(checksum)
    with lifecycle_write():
        record_event(
            "failback_package",
            "eligible" if session.status == OfflineSession.Status.ELIGIBLE else "reconciliation",
            session=session,
            details={"filename": package.name, "sha256": package_sha256},
        )
    return package, package_sha256


def inspect_failback_package(package_path, *, expected_sha256) -> tuple[dict, dict]:
    package_path = Path(package_path)
    if not package_path.is_file():
        raise FailbackError("Failback package не найден.")
    if not SHA256_RE.fullmatch(str(expected_sha256 or "")):
        raise FailbackError("Ожидаемый package SHA-256 отсутствует или некорректен.")
    if sha256_file(package_path) != expected_sha256:
        raise FailbackError("Package SHA-256 не совпадает.")
    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            if archive.testzip():
                raise FailbackError("Failback package повреждён.")
            names = set(archive.namelist())
            if "failback-report.json" not in names or "backup/manifest.json" not in names:
                raise FailbackError("Failback package не содержит обязательные metadata.")
            if archive.getinfo("failback-report.json").file_size > 2 * 1024 * 1024:
                raise FailbackError("Failback report слишком большой.")
            if archive.getinfo("backup/manifest.json").file_size > 2 * 1024 * 1024:
                raise FailbackError("Backup manifest слишком большой.")
            package_report = json.loads(archive.read("failback-report.json"))
            manifest = json.loads(archive.read("backup/manifest.json"))
            payload_names = {
                "failback-report.json",
                "backup/manifest.json",
                f"backup/{manifest.get('database_dump_filename', '')}",
            }
            if manifest.get("media_filename"):
                payload_names.add(f"backup/{manifest['media_filename']}")
            if not payload_names.issubset(names) or any(
                Path(name).is_absolute() or ".." in Path(name).parts or name not in payload_names
                for name in names
            ):
                raise FailbackError("Failback package содержит неожиданные файлы.")
            with tempfile.TemporaryDirectory(prefix="denstock-failback-") as temporary:
                run = Path(temporary)
                for name in names:
                    if not name.startswith("backup/"):
                        continue
                    target = run / Path(name).name
                    with archive.open(name) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                validation = validate_manifest(run, expected_source="emergency-local")
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise FailbackError("Failback package отсутствует или повреждён.") from exc
    if not validation.ok:
        raise FailbackError("Failback package invalid: " + "; ".join(validation.errors))
    if not isinstance(package_report, dict):
        raise FailbackError("Failback report должен быть JSON object.")
    return package_report, validation.manifest


def finalize_production_failback(*, package_path, expected_sha256):
    """Unlock an already restored production DB after independent package checks."""
    validate_database_target(mode="production")
    package_report, manifest = inspect_failback_package(
        package_path, expected_sha256=expected_sha256
    )
    if package_report.get("failback_status") != OfflineSession.Status.ELIGIBLE:
        raise FailbackError("Package не имеет failback status ELIGIBLE.")
    if package_report.get("automatic_production_overwrite") != "disabled":
        raise FailbackError("Package safety marker отсутствует.")
    session_id = package_report.get("offline_session_id")
    if (manifest.get("offline_lineage") or {}).get("offline_session_id") != session_id:
        raise FailbackError("Package session lineage не совпадает.")

    with transaction.atomic(), lifecycle_write():
        acquire_failover_lock(exclusive=True)
        state = DeploymentState.objects.select_for_update().get(pk=DeploymentState.SINGLETON_PK)
        if settings.DENSTOCK_MODE != "production":
            raise FailbackError("Finalizer разрешён только в production mode.")
        if state.write_state != DeploymentState.WriteState.EMERGENCY_FROZEN:
            raise FailbackError("Restored database не находится в emergency_frozen.")
        app_commit = settings.DENSTOCK_APP_COMMIT or backup._git_commit()
        if app_commit != manifest["app_commit"]:
            raise FailbackError("Production application commit не совпадает с package.")
        if migration_state()["fingerprint"] != manifest["migration_fingerprint"]:
            raise FailbackError("Production migration state не совпадает с package.")
        marker = business_state_marker()
        if marker["database_identity"] != manifest["database_identity"]:
            raise FailbackError("Restored database identity не совпадает с package.")
        if marker["business_sha256"] != manifest["data_state"]["business_sha256"]:
            raise FailbackError("Restored business data не совпадают с package.")
        if media_tree_sha256(settings.MEDIA_ROOT) != manifest["media_tree_sha256"]:
            raise FailbackError("Restored production media не совпадают с package.")
        session = OfflineSession.objects.select_for_update().filter(pk=session_id).first()
        if session is None:
            raise FailbackError("OfflineSession из package отсутствует в restored database.")
        session.status = OfflineSession.Status.COMPLETED
        session.ended_at = session.ended_at or timezone.now()
        session.final_backup_run_id = package_report.get("final_backup_run_id", "")[:128]
        session.final_manifest = manifest
        session.final_data_marker = manifest["data_state"]
        session.failback_report = package_report.get("failback_report") or {}
        session.save(
            update_fields=[
                "status",
                "ended_at",
                "final_backup_run_id",
                "final_manifest",
                "final_data_marker",
                "failback_report",
                "updated_at",
            ]
        )
        state.write_state = DeploymentState.WriteState.NORMAL
        state.state_reason = f"accepted-failback:{session.id}"
        state.state_changed_at = timezone.now()
        state.save(update_fields=["write_state", "state_reason", "state_changed_at", "updated_at"])
        record_event(
            "production_failback_finalized",
            "success",
            session=session,
            details={"package_sha256": expected_sha256},
        )
    return session


def complete_local_failback(production: dict, *, session=None, paths=None):
    """Close local lifecycle only after production proves this exact restore was accepted."""
    validate_database_target(mode="emergency-local")
    paths = paths or EmergencyPaths.configured()
    with control_lock(paths):
        session = (
            session or OfflineSession.objects.filter(status=OfflineSession.Status.ELIGIBLE).first()
        )
        if session is None:
            completed = OfflineSession.objects.filter(
                status=OfflineSession.Status.COMPLETED
            ).first()
            if completed:
                control = load_control(paths)
                control["offline_lifecycle"] = None
                save_control(control, paths)
                return completed
            raise FailbackError("Нет ELIGIBLE offline session.")
        final = session.final_manifest
        production_data = production.get("data_state") or {}
        checks = {
            "production mode": production.get("mode") == "production",
            "production writes enabled": (
                production.get("write_state") == DeploymentState.WriteState.NORMAL
            ),
            "accepted session": (
                production.get("state_reason") == f"accepted-failback:{session.id}"
            ),
            "stable probe": production.get("stable_snapshot") is True,
            "application commit": production.get("app_commit") == final.get("app_commit"),
            "migration state": (
                production.get("migration_fingerprint") == final.get("migration_fingerprint")
            ),
            "database identity": (
                production_data.get("database_identity") == final.get("database_identity")
            ),
            "business data": (
                production_data.get("business_sha256")
                == (final.get("data_state") or {}).get("business_sha256")
            ),
            "media": (production.get("media_tree_sha256") == final.get("media_tree_sha256")),
        }
        failed = [label for label, passed in checks.items() if not passed]
        if failed:
            raise FailbackError(
                "Production не подтвердил завершённый failback: " + ", ".join(failed)
            )
        with transaction.atomic(), lifecycle_write():
            acquire_failover_lock(exclusive=True)
            locked = OfflineSession.objects.select_for_update().get(pk=session.pk)
            state = DeploymentState.objects.select_for_update().get(pk=DeploymentState.SINGLETON_PK)
            if locked.status != OfflineSession.Status.ELIGIBLE:
                raise FailbackError("Offline session state изменился.")
            if state.write_state != DeploymentState.WriteState.EMERGENCY_FROZEN:
                raise FailbackError("Local database должна оставаться frozen.")
            locked.status = OfflineSession.Status.COMPLETED
            locked.failback_report = {
                **locked.failback_report,
                "production_acceptance_confirmed_at": timezone.now().isoformat(),
                "production_instance_id": production.get("instance_id", ""),
            }
            locked.save(update_fields=["status", "failback_report", "updated_at"])
            record_event(
                "local_failback_completed",
                "success",
                session=locked,
                details={"production_instance_id": production.get("instance_id", "")},
            )
        control = load_control(paths)
        control["offline_lifecycle"] = None
        save_control(control, paths)
        return locked


def prune_completed_artifacts(*, keep=None, root=None, paths=None) -> list[Path]:
    """Delete only old, accepted exports while preserving every unresolved session."""
    validate_database_target(mode="emergency-local")
    keep = settings.DENSTOCK_EMERGENCY_KEEP_COMPLETED_EXPORTS if keep is None else int(keep)
    if keep < 1:
        raise FailbackError("Нужно хранить минимум один completed export.")
    paths = paths or EmergencyPaths.configured()
    backup_base = (Path(root) if root else backup.backup_root()).resolve()
    completed = list(
        OfflineSession.objects.filter(status=OfflineSession.Status.COMPLETED).order_by(
            "-ended_at", "-started_at"
        )
    )
    candidates = completed[keep:]
    removed = []
    with control_lock(paths):
        for session in candidates:
            run = (backup_base / session.final_backup_run_id).resolve()
            if not session.final_backup_run_id or run.parent != backup_base or not run.is_dir():
                continue
            report = validate_manifest(run, expected_source="emergency-local")
            if not report.ok or (report.manifest.get("offline_lineage") or {}).get(
                "offline_session_id"
            ) != str(session.id):
                continue
            shutil.rmtree(run)
            removed.append(run)
            package = paths.root / "packages" / f"failback-{session.id}.zip"
            checksum = package.with_suffix(package.suffix + ".sha256")
            for artifact in (package, checksum):
                if artifact.is_file():
                    artifact.unlink()
                    removed.append(artifact)
        with lifecycle_write():
            record_event(
                "emergency_retention",
                "success",
                details={"keep": keep, "removed": [path.name for path in removed]},
            )
    return removed
