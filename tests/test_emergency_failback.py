import json
from copy import deepcopy
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.operations.emergency_manifest import SCHEMA_VERSION
from apps.operations.emergency_state import sha256_file
from apps.operations.failback import FailbackError, evaluate_failback, freeze_and_export
from apps.operations.management.commands.production_maintenance import (
    DISABLE_PHRASE,
    ENABLE_PHRASE,
)
from apps.operations.models import DeploymentState, OfflineSession
from apps.operations.write_guard import BusinessWriteBlocked
from apps.suppliers.models import Supplier

COMMIT = "a" * 40
MIGRATION_HASH = "b" * 64
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
            "database_identity": DATABASE_ID,
            "app_commit": COMMIT,
            "migration_fingerprint": MIGRATION_HASH,
        },
        base_data_marker=_base_data(),
        base_media_sha256=BASE_MEDIA_HASH,
        base_app_commit=COMMIT,
        base_migration_fingerprint=MIGRATION_HASH,
    )


def _final_run(root: Path, session: OfflineSession, **overrides):
    run = root / "2026-08-12_12-00-00"
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
        "stable_snapshot": True,
        "app_commit": COMMIT,
        "migration_fingerprint": MIGRATION_HASH,
        "data_state": _base_data(),
        "media_tree_sha256": BASE_MEDIA_HASH,
    }


@pytest.fixture
def active_export_runtime(settings, monkeypatch):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_INSTANCE_ID = "warehouse-pc"
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.EMERGENCY_ACTIVE
    state.save()
    session = _session(status=OfflineSession.Status.ACTIVE)
    monkeypatch.setattr(
        "apps.operations.failback.validate_database_target", lambda **kwargs: None
    )
    return state, session


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
def test_interrupted_freeze_requires_explicit_resume(
    tmp_path, active_export_runtime, monkeypatch
):
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
def test_corrupt_final_backup_is_blocked(tmp_path):
    session = _session()
    run = _final_run(tmp_path, session)
    (run / "db.dump").write_bytes(b"corrupt")

    decision = evaluate_failback(session, _production_probe(), run)

    assert decision.status == OfflineSession.Status.BLOCKED
    assert any("контрольная сумма" in reason for reason in decision.reasons)


@pytest.mark.django_db(transaction=True)
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
    monkeypatch.setattr(
        "apps.operations.views.media_tree_sha256", lambda path: BASE_MEDIA_HASH
    )
    url = reverse("operations:emergency_probe")

    assert client.get(url).status_code == 404
    response = client.get(url, HTTP_X_DENSTOCK_EMERGENCY_PROBE="probe-secret")

    assert response.status_code == 200
    assert response.json()["stable_snapshot"] is True
    assert response.json()["data_state"]["business_sha256"] == BASE_BUSINESS_HASH
    assert "no-store" in response.headers["Cache-Control"]
