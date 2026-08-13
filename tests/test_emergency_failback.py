import hashlib
import json
import zipfile
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.operations.emergency_manifest import SCHEMA_VERSION
from apps.operations.emergency_state import sha256_file
from apps.operations.failback import (
    FailbackError,
    complete_local_failback,
    configured_production_url,
    evaluate_failback,
    finalize_production_failback,
    freeze_and_export,
    inspect_failback_package,
    prepare_failback_package,
    prune_completed_artifacts,
)
from apps.operations.management.commands.production_maintenance import (
    DISABLE_PHRASE,
    ENABLE_PHRASE,
)
from apps.operations.models import DeploymentState, OfflineSession
from apps.operations.standby import EmergencyPaths, load_control, save_control
from apps.operations.write_guard import BusinessWriteBlocked
from apps.suppliers.models import Supplier

COMMIT = "a" * 40
MIGRATION_HASH = hashlib.sha256(b"[]").hexdigest()
BASE_BUSINESS_HASH = "c" * 64
BASE_MEDIA_HASH = "d" * 64
DATABASE_ID = "52347a14-d939-45e6-a397-06c79ef257f2"
BASE_RUN_ID = "d7919779-6c24-43cb-bb78-181f61a335d5"
TABLES = {
    "warehouse.stockmovement": {"count": 2, "max_pk": 2, "sha256": "1" * 64},
    "sales.sale": {"count": 1, "max_pk": 1, "sha256": "2" * 64},
    "inventory.inventory": {"count": 1, "max_pk": 1, "sha256": "3" * 64},
    "sales.reservation": {"count": 1, "max_pk": 1, "sha256": "4" * 64},
}


def _base_data():
    return {
        "database_identity": DATABASE_ID,
        "business_generation": 12,
        "business_sha256": BASE_BUSINESS_HASH,
        "tables": deepcopy(TABLES),
    }


def _session(*, status=OfflineSession.Status.FROZEN, instance_id="warehouse-pc"):
    return OfflineSession.objects.create(
        kind=OfflineSession.Kind.UNPLANNED,
        status=status,
        local_hostname="warehouse-pc",
        instance_id=instance_id,
        base_backup_run_id=BASE_RUN_ID,
        base_backup_created_at=timezone.now(),
        base_manifest={
            "backup_run_id": BASE_RUN_ID,
            "source_instance_id": "production",
            "database_identity": DATABASE_ID,
            "app_commit": COMMIT,
            "migration_fingerprint": MIGRATION_HASH,
        },
        base_data_marker=_base_data(),
        base_media_sha256=BASE_MEDIA_HASH,
        base_app_commit=COMMIT,
        base_migration_fingerprint=MIGRATION_HASH,
    )


def _final_run(
    root: Path,
    session: OfflineSession,
    *,
    run_name="2026-08-12_12-00-00",
    **overrides,
):
    run = root / run_name
    run.mkdir(parents=True, exist_ok=True)
    database = run / "db.dump"
    database.write_bytes(b"verified-final-dump")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "backup_run_id": "155f5606-813a-496c-b699-3554e39a96ea",
        "created_at": "2026-08-12T12:00:00+05:00",
        "verified_at": "2026-08-12T12:01:00+05:00",
        "source_environment": "emergency-local",
        "source_instance_id": session.instance_id,
        "app_commit": COMMIT,
        "database_name": "denstock_emergency_local",
        "database_identity": DATABASE_ID,
        "database_dump_filename": database.name,
        "database_sha256": sha256_file(database),
        "media_filename": None,
        "media_sha256": None,
        "media_tree_sha256": "e" * 64,
        "migration_fingerprint": MIGRATION_HASH,
        "migration_state": [],
        "data_state": {
            "database_identity": DATABASE_ID,
            "business_generation": 20,
            "business_sha256": "f" * 64,
            "tables": {},
        },
        "storage_origin": "emergency-local",
        "verification_status": "verified",
        "consistency": "single_writer_locked",
        "type": "emergency_final",
        "offline_lineage": {
            "offline_session_id": str(session.id),
            "base_backup_run_id": BASE_RUN_ID,
            "base_database_identity": DATABASE_ID,
            "base_business_sha256": BASE_BUSINESS_HASH,
            "base_media_sha256": BASE_MEDIA_HASH,
        },
    }
    manifest.update(overrides)
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


def _production_probe():
    return {
        "schema_version": 1,
        "mode": "production",
        "instance_id": "production",
        "write_state": DeploymentState.WriteState.MAINTENANCE,
        "state_reason": "controlled failover",
        "stable_snapshot": True,
        "app_commit": COMMIT,
        "migration_fingerprint": MIGRATION_HASH,
        "data_state": _base_data(),
        "media_tree_sha256": BASE_MEDIA_HASH,
    }


@override_settings(DENSTOCK_PRODUCTION_URL="https://production.example/")
def test_probe_token_is_pinned_to_configured_production_url():
    assert configured_production_url() == "https://production.example"
    assert configured_production_url("https://production.example/") == (
        "https://production.example"
    )
    with pytest.raises(FailbackError, match="token не отправлен"):
        configured_production_url("https://attacker.example")


@pytest.mark.parametrize(
    "url",
    [
        "http://production.example",
        "ftp://localhost",
        "https://user:password@production.example",
        "https://production.example/path",
        "https://production.example?target=other",
    ],
)
def test_probe_url_rejects_unsafe_configured_origin(settings, url):
    settings.DENSTOCK_PRODUCTION_URL = url

    with pytest.raises(FailbackError, match="root HTTPS URL"):
        configured_production_url()


@pytest.fixture
def active_export_runtime(tmp_path, settings, monkeypatch):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_INSTANCE_ID = "warehouse-pc"
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.EMERGENCY_ACTIVE
    state.save()
    session = _session(status=OfflineSession.Status.ACTIVE)
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "control"
    save_control(
        {
            "active_standby": {
                "database_name": "denstock_emergency_local",
                "backup_run_id": BASE_RUN_ID,
            },
            "previous_standbys": [],
        },
        EmergencyPaths(settings.DENSTOCK_EMERGENCY_ROOT),
    )
    monkeypatch.setattr("apps.operations.failback.validate_database_target", lambda **kwargs: None)
    return state, session


@pytest.mark.django_db
def test_freeze_creates_verified_final_export_and_blocks_writes(
    tmp_path, active_export_runtime, monkeypatch
):
    state, session = active_export_runtime
    monkeypatch.setattr(
        "apps.operations.failback.backup.backup_all",
        lambda **kwargs: _final_run(tmp_path, session),
    )

    frozen = freeze_and_export(root=tmp_path)

    state.refresh_from_db()
    assert frozen.status == OfflineSession.Status.FROZEN
    assert frozen.final_manifest["database_sha256"]
    assert frozen.final_manifest["media_tree_sha256"] == "e" * 64
    assert state.write_state == DeploymentState.WriteState.EMERGENCY_FROZEN
    with pytest.raises(BusinessWriteBlocked, match="blocked"):
        Supplier.objects.create(name="Too late")


@pytest.mark.django_db
def test_failed_export_stays_frozen_and_can_be_retried(
    tmp_path, active_export_runtime, monkeypatch
):
    state, session = active_export_runtime
    monkeypatch.setattr(
        "apps.operations.failback.backup.backup_all",
        lambda **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(FailbackError, match="disk full"):
        freeze_and_export(root=tmp_path)

    session.refresh_from_db()
    state.refresh_from_db()
    assert session.status == OfflineSession.Status.EXPORT_FAILED
    assert state.write_state == DeploymentState.WriteState.EMERGENCY_FROZEN

    monkeypatch.setattr(
        "apps.operations.failback.backup.backup_all",
        lambda **kwargs: _final_run(tmp_path, session),
    )
    assert freeze_and_export(root=tmp_path).status == OfflineSession.Status.FROZEN


@pytest.mark.django_db
def test_interrupted_freeze_requires_explicit_resume(tmp_path, active_export_runtime, monkeypatch):
    state, session = active_export_runtime
    session.status = OfflineSession.Status.FREEZING
    session.save()
    state.write_state = DeploymentState.WriteState.EMERGENCY_FROZEN
    state.save()
    monkeypatch.setattr(
        "apps.operations.failback.backup.backup_all",
        lambda **kwargs: _final_run(tmp_path, session),
    )

    with pytest.raises(FailbackError, match="--resume"):
        freeze_and_export(root=tmp_path)

    assert freeze_and_export(root=tmp_path, resume=True).status == OfflineSession.Status.FROZEN
    with pytest.raises(FailbackError, match="Нет active"):
        freeze_and_export(root=tmp_path)


@pytest.mark.django_db
def test_unchanged_production_and_common_ancestor_are_eligible(tmp_path):
    session = _session()
    run = _final_run(tmp_path, session)

    decision = evaluate_failback(session, _production_probe(), run)

    assert decision.status == OfflineSession.Status.ELIGIBLE
    assert decision.eligible
    assert decision.as_dict()["automatic_production_overwrite"] == "disabled"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "table",
    [
        "warehouse.stockmovement",
        "sales.sale",
        "inventory.inventory",
        "sales.reservation",
    ],
)
def test_changed_business_table_is_a_conflict(tmp_path, table):
    session = _session()
    run = _final_run(tmp_path, session)
    production = _production_probe()
    production["data_state"]["business_sha256"] = "0" * 64
    production["data_state"]["tables"][table]["count"] += 1

    decision = evaluate_failback(session, production, run)

    assert decision.status == OfflineSession.Status.CONFLICT
    assert table in decision.differences["business_tables"]


@pytest.mark.django_db
def test_generation_or_media_change_is_a_conflict(tmp_path):
    session = _session()
    run = _final_run(tmp_path, session)
    production = _production_probe()
    production["data_state"]["business_generation"] += 1
    production["media_tree_sha256"] = "0" * 64

    decision = evaluate_failback(session, production, run)

    assert decision.status == OfflineSession.Status.CONFLICT
    assert "business_generation" in decision.differences
    assert "media_tree_sha256" in decision.differences


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["app_commit", "migration_fingerprint"])
def test_incompatible_production_version_is_blocked(tmp_path, field):
    session = _session()
    run = _final_run(tmp_path, session)
    production = _production_probe()
    production[field] = "0" * len(production[field])

    decision = evaluate_failback(session, production, run)

    assert decision.status == OfflineSession.Status.BLOCKED


@pytest.mark.django_db
def test_different_production_instance_is_blocked(tmp_path):
    session = _session()
    run = _final_run(tmp_path, session)
    production = _production_probe()
    production["instance_id"] = "different-production"

    decision = evaluate_failback(session, production, run)

    assert decision.status == OfflineSession.Status.BLOCKED
    assert any("instance identity" in reason for reason in decision.reasons)


@pytest.mark.django_db
def test_final_backup_without_frozen_consistency_is_blocked(tmp_path):
    session = _session()
    run = _final_run(tmp_path, session, consistency="database_snapshot")

    decision = evaluate_failback(session, _production_probe(), run)

    assert decision.status == OfflineSession.Status.BLOCKED
    assert any("single-writer" in reason for reason in decision.reasons)


@pytest.mark.django_db
def test_corrupt_final_backup_is_blocked(tmp_path):
    session = _session()
    run = _final_run(tmp_path, session)
    (run / "db.dump").write_bytes(b"corrupt")

    decision = evaluate_failback(session, _production_probe(), run)

    assert decision.status == OfflineSession.Status.BLOCKED
    assert any("контрольная сумма" in reason for reason in decision.reasons)


@pytest.mark.django_db
def test_failback_package_contains_verified_export_and_report(tmp_path, settings, monkeypatch):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "runtime"
    session = _session(status=OfflineSession.Status.ELIGIBLE)
    run = _final_run(tmp_path / "backups", session)
    session.final_backup_run_id = run.name
    session.failback_report = {
        "status": OfflineSession.Status.ELIGIBLE,
        "automatic_production_overwrite": "disabled",
    }
    session.save()
    monkeypatch.setattr("apps.operations.failback.validate_database_target", lambda **kwargs: None)

    package, digest = prepare_failback_package(
        session=session,
        root=tmp_path / "backups",
        paths=EmergencyPaths(settings.DENSTOCK_EMERGENCY_ROOT),
    )

    assert sha256_file(package) == digest
    assert package.with_suffix(".zip.sha256").is_file()
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        assert "backup/manifest.json" in archive.namelist()
        metadata = json.loads(archive.read("failback-report.json"))
    assert metadata["failback_status"] == OfflineSession.Status.ELIGIBLE
    assert metadata["automatic_production_overwrite"] == "disabled"


@pytest.mark.django_db
def test_failback_package_rejects_wrong_sha(tmp_path, settings, monkeypatch):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "runtime"
    session = _session(status=OfflineSession.Status.CONFLICT)
    run = _final_run(tmp_path / "backups", session)
    session.final_backup_run_id = run.name
    session.save()
    monkeypatch.setattr("apps.operations.failback.validate_database_target", lambda **kwargs: None)
    package, _ = prepare_failback_package(session=session, root=tmp_path / "backups")

    with pytest.raises(FailbackError, match="SHA-256"):
        inspect_failback_package(package, expected_sha256="0" * 64)


@pytest.mark.django_db
def test_production_finalizer_unlocks_only_matching_eligible_restore(
    tmp_path, settings, monkeypatch
):
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_APP_COMMIT = COMMIT
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "runtime"
    session = _session(status=OfflineSession.Status.ELIGIBLE)
    run = _final_run(tmp_path / "backups", session)
    session.final_backup_run_id = run.name
    session.failback_report = {
        "status": OfflineSession.Status.ELIGIBLE,
        "automatic_production_overwrite": "disabled",
    }
    session.save()
    monkeypatch.setattr("apps.operations.failback.validate_database_target", lambda **kwargs: None)
    package, digest = prepare_failback_package(session=session, root=tmp_path / "backups")
    state = DeploymentState.get_solo()
    state.database_identity = DATABASE_ID
    state.write_state = DeploymentState.WriteState.EMERGENCY_FROZEN
    state.save()
    monkeypatch.setattr(
        "apps.operations.failback.migration_state",
        lambda: {"fingerprint": MIGRATION_HASH},
    )
    monkeypatch.setattr(
        "apps.operations.failback.business_state_marker",
        lambda: {
            "database_identity": DATABASE_ID,
            "business_sha256": "f" * 64,
        },
    )
    monkeypatch.setattr("apps.operations.failback.media_tree_sha256", lambda path: "e" * 64)

    finalized = finalize_production_failback(package_path=package, expected_sha256=digest)

    state.refresh_from_db()
    assert finalized.pk == session.pk
    assert finalized.status == OfflineSession.Status.COMPLETED
    assert state.write_state == DeploymentState.WriteState.NORMAL


@pytest.mark.django_db
def test_production_finalizer_refuses_conflict_package(tmp_path, settings, monkeypatch):
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_APP_COMMIT = COMMIT
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "runtime"
    session = _session(status=OfflineSession.Status.CONFLICT)
    run = _final_run(tmp_path / "backups", session)
    session.final_backup_run_id = run.name
    session.save()
    monkeypatch.setattr("apps.operations.failback.validate_database_target", lambda **kwargs: None)
    package, digest = prepare_failback_package(session=session, root=tmp_path / "backups")
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.EMERGENCY_FROZEN
    state.save()

    with pytest.raises(FailbackError, match="ELIGIBLE"):
        finalize_production_failback(package_path=package, expected_sha256=digest)

    state.refresh_from_db()
    assert state.write_state == DeploymentState.WriteState.EMERGENCY_FROZEN


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="production")
def test_production_maintenance_transitions_require_explicit_commands():
    state = DeploymentState.get_solo()

    call_command("production_maintenance", enable=True, confirm=ENABLE_PHRASE)
    state.refresh_from_db()
    assert state.write_state == DeploymentState.WriteState.MAINTENANCE

    call_command("production_maintenance", disable=True, confirm=DISABLE_PHRASE)
    state.refresh_from_db()
    assert state.write_state == DeploymentState.WriteState.NORMAL


@pytest.mark.django_db
@override_settings(
    DENSTOCK_MODE="production",
    DENSTOCK_INSTANCE_ID="production-test",
    DENSTOCK_APP_COMMIT=COMMIT,
    DENSTOCK_EMERGENCY_PROBE_TOKEN="probe-secret",
)
def test_production_probe_requires_token_and_reports_stable_marker(client, monkeypatch):
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.MAINTENANCE
    state.save()
    monkeypatch.setattr("apps.operations.views.business_state_marker", _base_data)
    monkeypatch.setattr(
        "apps.operations.views.migration_state", lambda: {"fingerprint": MIGRATION_HASH}
    )
    monkeypatch.setattr("apps.operations.views.media_tree_sha256", lambda path: BASE_MEDIA_HASH)
    url = reverse("operations:emergency_probe")

    assert client.get(url).status_code == 404
    response = client.get(url, HTTP_X_DENSTOCK_EMERGENCY_PROBE="probe-secret")

    assert response.status_code == 200
    assert response.json()["stable_snapshot"] is True
    assert response.json()["data_state"]["business_sha256"] == BASE_BUSINESS_HASH
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.django_db
def test_completed_failback_clears_control_lifecycle(tmp_path, settings, monkeypatch):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "runtime"
    paths = EmergencyPaths(settings.DENSTOCK_EMERGENCY_ROOT)
    session = _session(status=OfflineSession.Status.ELIGIBLE)
    session.final_manifest = {
        "app_commit": COMMIT,
        "migration_fingerprint": MIGRATION_HASH,
        "database_identity": DATABASE_ID,
        "media_tree_sha256": "e" * 64,
        "data_state": {"business_sha256": "f" * 64},
    }
    session.save()
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.EMERGENCY_FROZEN
    state.save()
    save_control(
        {
            "active_standby": {"database_name": "denstock_emergency_local"},
            "previous_standbys": [],
            "offline_lifecycle": {"session_id": str(session.id), "status": "eligible"},
        },
        paths,
    )
    monkeypatch.setattr("apps.operations.failback.validate_database_target", lambda **kwargs: None)
    production = {
        "mode": "production",
        "instance_id": "production",
        "write_state": DeploymentState.WriteState.NORMAL,
        "state_reason": f"accepted-failback:{session.id}",
        "stable_snapshot": True,
        "app_commit": COMMIT,
        "migration_fingerprint": MIGRATION_HASH,
        "data_state": {
            "database_identity": DATABASE_ID,
            "business_sha256": "f" * 64,
        },
        "media_tree_sha256": "e" * 64,
    }

    completed = complete_local_failback(production, session=session, paths=paths)

    assert completed.status == OfflineSession.Status.COMPLETED
    assert load_control(paths)["offline_lifecycle"] is None


@pytest.mark.django_db
def test_completed_failback_refuses_wrong_production_data(tmp_path, settings, monkeypatch):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "runtime"
    paths = EmergencyPaths(settings.DENSTOCK_EMERGENCY_ROOT)
    session = _session(status=OfflineSession.Status.ELIGIBLE)
    session.final_manifest = {
        "app_commit": COMMIT,
        "migration_fingerprint": MIGRATION_HASH,
        "database_identity": DATABASE_ID,
        "media_tree_sha256": "e" * 64,
        "data_state": {"business_sha256": "f" * 64},
    }
    session.save()
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.EMERGENCY_FROZEN
    state.save()
    save_control(
        {
            "active_standby": {"database_name": "denstock_emergency_local"},
            "previous_standbys": [],
            "offline_lifecycle": {"session_id": str(session.id), "status": "eligible"},
        },
        paths,
    )
    monkeypatch.setattr("apps.operations.failback.validate_database_target", lambda **kwargs: None)
    production = {
        "mode": "production",
        "write_state": DeploymentState.WriteState.NORMAL,
        "state_reason": f"accepted-failback:{session.id}",
        "stable_snapshot": True,
        "app_commit": COMMIT,
        "migration_fingerprint": MIGRATION_HASH,
        "data_state": {
            "database_identity": DATABASE_ID,
            "business_sha256": "0" * 64,
        },
        "media_tree_sha256": "e" * 64,
    }

    with pytest.raises(FailbackError, match="business data"):
        complete_local_failback(production, session=session, paths=paths)

    session.refresh_from_db()
    assert session.status == OfflineSession.Status.ELIGIBLE
    assert load_control(paths)["offline_lifecycle"] is not None


@pytest.mark.django_db
def test_retention_deletes_only_old_completed_exports(tmp_path, settings, monkeypatch):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "runtime"
    monkeypatch.setattr("apps.operations.failback.validate_database_target", lambda **kwargs: None)
    older = _session(status=OfflineSession.Status.COMPLETED, instance_id="warehouse-old")
    older.ended_at = timezone.now() - timedelta(days=2)
    older_run = _final_run(tmp_path / "backups", older, run_name="2026-08-10_12-00-00")
    older.final_backup_run_id = older_run.name
    older.save()
    newer = _session(status=OfflineSession.Status.COMPLETED, instance_id="warehouse-new")
    newer.ended_at = timezone.now() - timedelta(days=1)
    newer_run = _final_run(tmp_path / "backups", newer, run_name="2026-08-11_12-00-00")
    newer.final_backup_run_id = newer_run.name
    newer.save()
    unknown = tmp_path / "backups" / "unknown"
    unknown.mkdir()
    (unknown / "manifest.json").write_text("{}", encoding="utf-8")

    removed = prune_completed_artifacts(
        keep=1,
        root=tmp_path / "backups",
        paths=EmergencyPaths(settings.DENSTOCK_EMERGENCY_ROOT),
    )

    assert older_run in removed
    assert not older_run.exists()
    assert newer_run.exists()
    assert unknown.exists()
