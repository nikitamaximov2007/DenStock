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


# --- Hotfix 2: старые архивы pg_dump 17 (формат 1.16) --------------------------------

UNSUPPORTED_STDERR = "pg_restore: error: unsupported version (1.16) in file header\n"


@pytest.fixture
def pg_bins(tmp_path, monkeypatch):
    """Явные versioned-клиенты 16 и 17; unversioned PATH пуст (запрещён)."""
    for version in (16, 17):
        d = tmp_path / "pg" / str(version)
        d.mkdir(parents=True)
        (d / "pg_dump").write_text("")
        (d / "pg_restore").write_text("")
    monkeypatch.setattr(
        backup_mod, "PG_BIN_TEMPLATE", str(tmp_path / "pg" / "{version}" / "{name}")
    )
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: None)
    return tmp_path / "pg"


class _RunRecorder:
    """Подменяет subprocess.run: пишет команды, отдаёт заготовленные результаты."""

    def __init__(self, results):
        self.results = list(results)
        self.commands = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        return self.results.pop(0)


def test_backup_db_uses_pg_dump_16_explicitly(pg_bins, tmp_path, monkeypatch):
    recorder = _RunRecorder([_completed(0)])
    monkeypatch.setattr(backup_mod.subprocess, "run", recorder)
    backup_mod.backup_db(tmp_path / "out", settings_dict=PG_SETTINGS)
    assert recorder.commands[0][0] == str(pg_bins / "16" / "pg_dump")


def test_restore_uses_versioned_pg_restore_16_first(pg_bins, dump_file, monkeypatch):
    recorder = _RunRecorder([_completed(0)])
    monkeypatch.setattr(backup_mod.subprocess, "run", recorder)
    assert restore_db(dump_file, settings_dict=PG_SETTINGS) == []
    assert recorder.commands[0][0] == str(pg_bins / "16" / "pg_restore")


def test_unsupported_archive_falls_back_to_pg_restore_17(pg_bins, dump_file, monkeypatch):
    recorder = _RunRecorder([_completed(1, UNSUPPORTED_STDERR), _completed(0)])
    monkeypatch.setattr(backup_mod.subprocess, "run", recorder)
    warnings = restore_db(dump_file, settings_dict=PG_SETTINGS)
    assert recorder.commands[0][0] == str(pg_bins / "16" / "pg_restore")
    assert recorder.commands[1][0] == str(pg_bins / "17" / "pg_restore")
    assert len(warnings) == 1 and "pg_restore 17" in warnings[0]


def test_fallback_plus_transaction_timeout_is_warning(pg_bins, dump_file, monkeypatch):
    recorder = _RunRecorder([_completed(1, UNSUPPORTED_STDERR), _completed(1, KNOWN_STDERR)])
    monkeypatch.setattr(backup_mod.subprocess, "run", recorder)
    warnings = restore_db(dump_file, settings_dict=PG_SETTINGS)
    assert len(warnings) == 2
    assert "pg_restore 17" in warnings[0]
    assert "transaction_timeout" in warnings[1]


def test_fallback_other_error_stays_fatal(pg_bins, dump_file, monkeypatch):
    recorder = _RunRecorder([_completed(1, UNSUPPORTED_STDERR), _completed(1, OTHER_STDERR)])
    monkeypatch.setattr(backup_mod.subprocess, "run", recorder)
    with pytest.raises(OperationsError):
        restore_db(dump_file, settings_dict=PG_SETTINGS)


def test_unsupported_archive_without_client17_is_clear_error(
    tmp_path, dump_file, monkeypatch
):
    d16 = tmp_path / "pg" / "16"
    d16.mkdir(parents=True)
    (d16 / "pg_restore").write_text("")  # клиента 17 нет
    monkeypatch.setattr(
        backup_mod, "PG_BIN_TEMPLATE", str(tmp_path / "pg" / "{version}" / "{name}")
    )
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        backup_mod.subprocess, "run",
        _RunRecorder([_completed(1, UNSUPPORTED_STDERR)]),
    )
    with pytest.raises(OperationsError, match="postgresql-client-17"):
        restore_db(dump_file, settings_dict=PG_SETTINGS)


def test_ops_check_reports_client_paths_and_versions(pg_bins, db, settings, tmp_path, monkeypatch):
    from apps.operations import checks

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.BACKUP_ROOT = tmp_path / "backups"
    monkeypatch.setattr(checks, "_client_version", lambda b: "pg (PostgreSQL) x.y")
    results = checks.run_checks(settings_dict=PG_SETTINGS)
    pg = next(r for r in results if r.name == "Клиенты PostgreSQL")
    assert pg.level == checks.OK
    assert str(pg_bins / "16" / "pg_dump") in pg.message
    assert str(pg_bins / "16" / "pg_restore") in pg.message
    assert str(pg_bins / "17" / "pg_restore") in pg.message
    assert "PostgreSQL" in pg.message  # версии, не просто «доступны»


def test_verify_warns_on_pg17_archive_header(backups_root):
    # Заголовок custom-архива pg_dump 17: "PGDMP" + vmaj=1, vmin=16.
    _make_run(backups_root, db_bytes=b"PGDMP" + bytes([1, 16, 1]) + b"rest-of-dump")
    report = verify_backup("2026-07-05_07-13-35")
    assert report.ok  # предупреждение, не ошибка: fallback восстановит
    assert any("pg_restore 17" in w for w in report.warnings)


def test_verify_quiet_on_pg16_archive_header(backups_root):
    # Архив pg_dump 16 (формат 1.15): предупреждения о формате нет.
    _make_run(backups_root, db_bytes=b"PGDMP" + bytes([1, 15, 0]) + b"rest-of-dump")
    report = verify_backup("2026-07-05_07-13-35")
    assert report.ok
    assert not any("pg_restore 17" in w for w in report.warnings)


# --- Dockerfile: оба клиента (16 для дампов, 17 для старых архивов) ------------------


def test_dockerfile_pins_postgresql_client_16():
    text = (Path(django_settings.BASE_DIR) / "docker" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "postgresql-client-16" in text
    assert "postgresql-client-17" in text  # чтение старых архивов pg_dump 17
    # Unversioned пакет больше не ставится (иначе снова приедет клиент 17 в PATH).
    assert "postgresql-client \\" not in text
