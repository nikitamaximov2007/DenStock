"""Layer 30 hotfix — совместимость restore с PostgreSQL 16 (transaction_timeout).

Гарантии: restore_db терпит СТРОГО одну известную безвредную ошибку pg_restore
(SET transaction_timeout из дампа клиента новее сервера) и возвращает её как
предупреждение; любая другая ошибка pg_restore остаётся фатальной; verify_backup
заранее предупреждает о такой несовместимости; web-restore пишет предупреждение
в журнал; Dockerfile пинует postgresql-client-16 (клиент = сервер).
"""
import json
import subprocess
from pathlib import Path

import pytest
from django.conf import settings as django_settings

from apps.operations import backup as backup_mod
from apps.operations import restore as restore_mod
from apps.operations.backup import OperationsError, pg_restore_error_filter, restore_db
from apps.operations.models import RestoreJob
from apps.operations.restore import run_web_restore, verify_backup

# Реальный stderr из disaster recovery drill (denstock_restore_test).
KNOWN_STDERR = (
    "pg_restore: error: could not execute query: ERROR: "
    'unrecognized configuration parameter "transaction_timeout"\n'
    "Command was: SET transaction_timeout = 0;\n"
    "pg_restore: warning: errors ignored on restore: 1\n"
)
OTHER_STDERR = (
    'pg_restore: error: could not execute query: ERROR: relation "x" already exists\n'
)

PG_SETTINGS = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "denstock", "USER": "denstock", "PASSWORD": "secret",
    "HOST": "db", "PORT": "5432",
}


# --- Фильтр ошибок pg_restore -----------------------------------------------------


def test_filter_only_transaction_timeout_is_harmless():
    fatal, harmless = pg_restore_error_filter(KNOWN_STDERR)
    assert fatal == []
    assert len(harmless) == 1


def test_filter_other_error_is_fatal():
    fatal, harmless = pg_restore_error_filter(OTHER_STDERR)
    assert len(fatal) == 1
    assert harmless == []


def test_filter_mixed_errors_stay_fatal():
    fatal, harmless = pg_restore_error_filter(KNOWN_STDERR + OTHER_STDERR)
    assert len(fatal) == 1  # чужая ошибка не прощается из-за известной
    assert len(harmless) == 1


def test_filter_empty_stderr():
    assert pg_restore_error_filter("") == ([], [])


# --- restore_db (Postgres-ветка с подменённым subprocess) ---------------------------


@pytest.fixture
def dump_file(tmp_path):
    path = tmp_path / "db.dump"
    path.write_bytes(b"PGDMP fake")
    return path


def _completed(returncode, stderr=""):
    return subprocess.CompletedProcess(args=["pg_restore"], returncode=returncode,
                                       stdout="", stderr=stderr)


def test_restore_db_clean_success_no_warnings(dump_file, monkeypatch):
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: "/usr/bin/pg_restore")
    monkeypatch.setattr(backup_mod.subprocess, "run", lambda *a, **kw: _completed(0))
    assert restore_db(dump_file, settings_dict=PG_SETTINGS) == []


def test_restore_db_tolerates_known_transaction_timeout(dump_file, monkeypatch):
    """Ровно случай из drill: единственная ошибка — transaction_timeout."""
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: "/usr/bin/pg_restore")
    monkeypatch.setattr(
        backup_mod.subprocess, "run", lambda *a, **kw: _completed(1, KNOWN_STDERR)
    )
    warnings = restore_db(dump_file, settings_dict=PG_SETTINGS)
    assert len(warnings) == 1
    assert "transaction_timeout" in warnings[0]


def test_restore_db_fails_on_other_error(dump_file, monkeypatch):
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: "/usr/bin/pg_restore")
    monkeypatch.setattr(
        backup_mod.subprocess, "run", lambda *a, **kw: _completed(1, OTHER_STDERR)
    )
    with pytest.raises(OperationsError):
        restore_db(dump_file, settings_dict=PG_SETTINGS)


def test_restore_db_fails_on_mixed_errors(dump_file, monkeypatch):
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: "/usr/bin/pg_restore")
    monkeypatch.setattr(
        backup_mod.subprocess, "run",
        lambda *a, **kw: _completed(1, KNOWN_STDERR + OTHER_STDERR),
    )
    with pytest.raises(OperationsError):
        restore_db(dump_file, settings_dict=PG_SETTINGS)


def test_restore_db_fails_on_nonzero_without_recognized_errors(dump_file, monkeypatch):
    """Ненулевой код без распознанных строк ошибок — фатально (ничего не глотаем)."""
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: "/usr/bin/pg_restore")
    monkeypatch.setattr(
        backup_mod.subprocess, "run", lambda *a, **kw: _completed(2, "boom")
    )
    with pytest.raises(OperationsError):
        restore_db(dump_file, settings_dict=PG_SETTINGS)


# --- verify_backup предупреждает заранее --------------------------------------------


@pytest.fixture
def backups_root(settings, tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    settings.BACKUP_ROOT = root
    return root


def _make_run(root, run_id="2026-07-05_07-13-35", db_bytes=b"PGDMP data"):
    run = Path(root) / run_id
    run.mkdir(parents=True)
    (run / "db.dump").write_bytes(db_bytes)
    (run / "manifest.json").write_text(json.dumps({
        "created_at": "2026-07-05T07:13:35", "type": "manual", "engine": "sqlite",
        "db_file": "db.dump", "media_file": None, "version": None,
    }), encoding="utf-8")
    return run


def test_verify_warns_on_transaction_timeout_in_dump(backups_root):
    _make_run(backups_root, db_bytes=b"PGDMP ... SET transaction_timeout = 0; ...")
    report = verify_backup("2026-07-05_07-13-35")
    assert report.ok  # предупреждение, не ошибка
    assert any("transaction_timeout" in w for w in report.warnings)


def test_verify_quiet_on_clean_dump(backups_root):
    _make_run(backups_root)
    report = verify_backup("2026-07-05_07-13-35")
    assert report.ok
    assert not any("transaction_timeout" in w for w in report.warnings)


# --- Web-restore использует тот же путь и пишет предупреждение в журнал --------------


def test_web_restore_logs_transaction_timeout_warning(
    backups_root, db, django_user_model, monkeypatch
):
    user = django_user_model.objects.create_superuser(
        username="owner", password="x", email="nikita.maximov2007@gmail.com"
    )
    _make_run(backups_root)
    pre = _make_run(backups_root, run_id="2026-07-05_08-00-00")
    monkeypatch.setattr(backup_mod, "backup_all", lambda **kw: pre)
    monkeypatch.setattr(
        restore_mod.backup, "restore_db",
        lambda p: ["пропущен SET transaction_timeout: дамп сделан клиентом новее"],
    )
    monkeypatch.setattr(restore_mod, "call_command", lambda *a, **kw: None)
    job = run_web_restore("2026-07-05_07-13-35", user=user)
    assert job.status == RestoreJob.Status.COMPLETED
    assert "transaction_timeout" in job.log


# --- Dockerfile: клиент = сервер (16) ------------------------------------------------


def test_dockerfile_pins_postgresql_client_16():
    text = (Path(django_settings.BASE_DIR) / "docker" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "postgresql-client-16" in text
    # Unversioned пакет больше не ставится (иначе снова приедет клиент 17).
    assert "postgresql-client \\" not in text
