"""Сквозной A/B через НАСТОЯЩИЙ шлюз активации на PostgreSQL.

Отличие от `test_emergency_ab_adversarial`: там решение собирается из продуктовых
примитивов, здесь его целиком принимает `start_offline_session`, то есть та же
функция, которую вызывает складской компьютер.

Подменяется только файловая обвязка слота standby: имя слота обязано быть 12
hex-символами и совпадать с именем базы, что для тестовой базы недостижимо.
Подмена возвращает manifest ТОЛЬКО после настоящей проверки
`validate_manifest(..., expected_source="production")`, поэтому проверка подписи,
сверка назначенной станции и эпохи остаются продуктовыми.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from django.db import connection

from apps.operations.emergency_lifecycle import (
    EmergencyLifecycleError,
    start_offline_session,
)
from apps.operations.emergency_manifest import validate_manifest
from apps.operations.emergency_state import (
    business_state_marker,
    migration_state,
    sha256_file,
)
from apps.operations.models import DeploymentState, OfflineSession
from apps.operations.standby import EmergencyPaths, save_control
from tests.emergency_support import configure_test_trust, sign_production_manifest

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="Emergency activation gate integration test",
    ),
]

COMMIT = "c" * 40
UUID_A = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
UUID_B = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")


def _reset_state() -> DeploymentState:
    OfflineSession.objects.all().delete()
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.NORMAL
    state.state_reason = ""
    state.save(update_fields=["write_state", "state_reason", "updated_at"])
    return state


def _emergency_settings(settings, tmp_path, *, workstation_id):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_INSTANCE_ID = "ab-activation-test"
    settings.DENSTOCK_APP_COMMIT = COMMIT
    settings.DENSTOCK_EMERGENCY_DB_PREFIX = str(connection.settings_dict["NAME"])
    settings.DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS = [connection.settings_dict["HOST"]]
    settings.DENSTOCK_PRODUCTION_DB_HOSTS = []
    settings.DENSTOCK_EMERGENCY_ROLE = "primary"
    configure_test_trust(tmp_path, settings, workstation_id=workstation_id)
    identity = tmp_path / "workstation-id.txt"
    identity.write_text(str(workstation_id), encoding="utf-8")
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID_PATH = str(identity)
    return identity


def _signed_standby(tmp_path, settings, state, *, authorized_id, epoch=1, tamper=None) -> Path:
    run = tmp_path / "standby-run"
    run.mkdir(parents=True, exist_ok=True)
    dump = run / "db.dump"
    dump.write_bytes(b"synthetic-production-dump")
    migrations = migration_state()
    manifest = {
        "schema_version": 2,
        "backup_run_id": str(uuid.uuid4()),
        "created_at": "2026-08-12T10:00:00+05:00",
        "verified_at": "2026-08-12T10:01:00+05:00",
        "source_environment": "production",
        "source_instance_id": "production",
        "authorized_emergency_primary_id": str(authorized_id),
        "primary_authorization_epoch": epoch,
        "app_commit": COMMIT,
        "database_name": "denstock",
        "database_identity": str(state.database_identity),
        "database_dump_filename": "db.dump",
        "database_sha256": sha256_file(dump),
        "media_filename": None,
        "media_sha256": None,
        "media_tree_sha256": "d" * 64,
        "migration_fingerprint": migrations["fingerprint"],
        "migration_state": migrations["applied"],
        "data_state": business_state_marker(),
        "storage_origin": "yandex-object-storage",
        "verification_status": "verified",
        "consistency": "database_snapshot",
    }
    sign_production_manifest(manifest, settings)
    if tamper:
        manifest.update(tamper)
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


def _install_standby(monkeypatch, run: Path):
    """Отдать шлюзу manifest только после НАСТОЯЩЕЙ проверки подписи."""

    def _active_standby(paths=None):
        report = validate_manifest(run, expected_source="production")
        if not report.ok:
            raise EmergencyLifecycleError(
                "Standby manifest invalid: " + "; ".join(report.errors)
            )
        return {"database_name": connection.settings_dict["NAME"]}, report.manifest

    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby", _active_standby
    )


def _paths(tmp_path):
    paths = EmergencyPaths(tmp_path / "emergency")
    save_control({"active_standby": {}, "previous_standbys": []}, paths)
    return paths


def _start(paths):
    return start_offline_session(kind=OfflineSession.Kind.UNPLANNED, paths=paths)


def test_authorized_workstation_reaches_active(tmp_path, settings, monkeypatch):
    """Назначенная станция с подлинной подписанной копией доходит до ACTIVE."""
    state = _reset_state()
    _emergency_settings(settings, tmp_path, workstation_id=UUID_A)
    _install_standby(monkeypatch, _signed_standby(tmp_path, settings, state, authorized_id=UUID_A))
    try:
        session = _start(_paths(tmp_path))
        assert session.status == OfflineSession.Status.ACTIVE
        state.refresh_from_db()
        assert state.write_state == DeploymentState.WriteState.EMERGENCY_ACTIVE
    finally:
        settings.DENSTOCK_MODE = "test"


def test_other_workstation_never_reaches_active(tmp_path, settings, monkeypatch):
    """Та же подлинная копия на станции B: ACTIVE не достигается."""
    state = _reset_state()
    _emergency_settings(settings, tmp_path, workstation_id=UUID_B)
    _install_standby(monkeypatch, _signed_standby(tmp_path, settings, state, authorized_id=UUID_A))
    try:
        with pytest.raises(EmergencyLifecycleError, match="другого аварийного"):
            _start(_paths(tmp_path))
        state.refresh_from_db()
        assert state.write_state == DeploymentState.WriteState.NORMAL
        assert not OfflineSession.objects.exists()
    finally:
        settings.DENSTOCK_MODE = "test"


def test_zero_epoch_never_reaches_active(tmp_path, settings, monkeypatch):
    """Primary не назначен: эпоха 0, активация закрыта."""
    state = _reset_state()
    _emergency_settings(settings, tmp_path, workstation_id=UUID_A)
    _install_standby(
        monkeypatch,
        _signed_standby(tmp_path, settings, state, authorized_id=UUID_A, epoch=0),
    )
    try:
        with pytest.raises(EmergencyLifecycleError, match="устарела|отсутствует"):
            _start(_paths(tmp_path))
        state.refresh_from_db()
        assert state.write_state == DeploymentState.WriteState.NORMAL
    finally:
        settings.DENSTOCK_MODE = "test"


def test_tampered_authorization_is_rejected_by_the_signature(tmp_path, settings, monkeypatch):
    """UUID подменён после подписи: настоящая проверка подписи не пропускает."""
    state = _reset_state()
    _emergency_settings(settings, tmp_path, workstation_id=UUID_B)
    _install_standby(
        monkeypatch,
        _signed_standby(
            tmp_path, settings, state,
            authorized_id=UUID_A,
            tamper={"authorized_emergency_primary_id": str(UUID_B)},
        ),
    )
    try:
        with pytest.raises(EmergencyLifecycleError, match="Standby manifest invalid"):
            _start(_paths(tmp_path))
        state.refresh_from_db()
        assert state.write_state == DeploymentState.WriteState.NORMAL
    finally:
        settings.DENSTOCK_MODE = "test"


def test_secondary_role_never_reaches_active(tmp_path, settings, monkeypatch):
    state = _reset_state()
    _emergency_settings(settings, tmp_path, workstation_id=UUID_A)
    settings.DENSTOCK_EMERGENCY_ROLE = "secondary"
    _install_standby(monkeypatch, _signed_standby(tmp_path, settings, state, authorized_id=UUID_A))
    try:
        with pytest.raises(EmergencyLifecycleError, match="secondary"):
            _start(_paths(tmp_path))
        state.refresh_from_db()
        assert state.write_state == DeploymentState.WriteState.NORMAL
    finally:
        settings.DENSTOCK_MODE = "test"


def test_identity_file_mismatch_never_reaches_active(tmp_path, settings, monkeypatch):
    """Конфигурация подделана под A, защищённый файл остался B."""
    state = _reset_state()
    identity = _emergency_settings(settings, tmp_path, workstation_id=UUID_B)
    identity.write_text(str(UUID_B), encoding="utf-8")
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID = str(UUID_A)
    _install_standby(monkeypatch, _signed_standby(tmp_path, settings, state, authorized_id=UUID_A))
    try:
        with pytest.raises(EmergencyLifecycleError, match="не назначен"):
            _start(_paths(tmp_path))
        state.refresh_from_db()
        assert state.write_state == DeploymentState.WriteState.NORMAL
    finally:
        settings.DENSTOCK_MODE = "test"
