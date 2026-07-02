"""Слой 25 — локальная эксплуатация и резервное копирование.

Ключевой инвариант: эксплуатационный слой НЕ трогает складскую физику. Тестируем
функции `apps.operations.backup`/`checks` на временных каталогах и команды через
`call_command` — без зависимости от боевой БД.
"""
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.inventory.models import StockBalance, StockMovement
from apps.operations import backup, checks

BASE_DIR = Path(django_settings.BASE_DIR)


def _sqlite_settings(path) -> dict:
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)}


def _postgres_settings() -> dict:
    return {
        "ENGINE": "django.db.backends.postgresql", "NAME": "denstock",
        "USER": "denstock", "PASSWORD": "secret-xyz", "HOST": "db", "PORT": "5432",
    }


def _make_db_file(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


# --- Backup media ------------------------------------------------------------


def test_backup_media_creates_archive(tmp_path):
    media = tmp_path / "media"
    (media / "part-types" / "1").mkdir(parents=True)
    (media / "part-types" / "1" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    dest = tmp_path / "run"
    archive = backup.backup_media(dest, media_root=media)
    assert archive is not None and archive.exists()
    assert archive.name == "media.tar.gz"
    with tarfile.open(archive) as tar:
        assert any("a.png" in n for n in tar.getnames())


def test_backup_media_missing_dir_no_crash(tmp_path):
    assert backup.backup_media(tmp_path / "run", media_root=tmp_path / "nope") is None


def test_backup_media_empty_dir_no_crash(tmp_path):
    (tmp_path / "media").mkdir()
    assert backup.backup_media(tmp_path / "run", media_root=tmp_path / "media") is None


# --- Backup db ---------------------------------------------------------------


def test_backup_db_sqlite_copies_file(tmp_path):
    db = _make_db_file(tmp_path / "src.sqlite3")
    dest = tmp_path / "run"
    out = backup.backup_db(dest, settings_dict=_sqlite_settings(db))
    assert out.exists() and out.name == "db.sqlite3"
    assert out.read_bytes() == db.read_bytes()


def test_backup_db_postgres_without_pg_dump_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)
    with pytest.raises(backup.OperationsError, match="pg_dump"):
        backup.backup_db(tmp_path / "run", settings_dict=_postgres_settings())


def test_backup_db_postgres_builds_command_without_password(tmp_path, monkeypatch):
    captured = {}

    monkeypatch.setattr(backup.shutil, "which", lambda _name: "/usr/bin/pg_dump")

    def fake_run(cmd, env=None, check=None, capture_output=None, text=None):
        captured["cmd"] = cmd
        captured["env"] = env
        Path(cmd[cmd.index("-f") + 1]).write_bytes(b"PGDMP")  # эмулируем дамп

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    out = backup.backup_db(tmp_path / "run", settings_dict=_postgres_settings())
    assert out.exists()
    # Пароль не в argv, а в окружении (PGPASSWORD).
    assert "secret-xyz" not in " ".join(captured["cmd"])
    assert captured["env"]["PGPASSWORD"] == "secret-xyz"
    assert "pg_dump" in captured["cmd"][0]


# --- Backup all + manifest ---------------------------------------------------


def test_backup_all_structure_and_manifest(tmp_path):
    db = _make_db_file(tmp_path / "src.sqlite3")
    media = tmp_path / "media"
    media.mkdir()
    (media / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    run = backup.backup_all(
        root=tmp_path / "backups", settings_dict=_sqlite_settings(db), media_root=media,
    )
    assert (run / "db.sqlite3").exists()
    assert (run / "media.tar.gz").exists()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["engine"] == "sqlite"
    assert manifest["db_file"] == "db.sqlite3"
    assert manifest["type"] == "manual"  # дефолтный trigger
    # Секреты в манифест не попадают.
    assert "secret" not in json.dumps(manifest).lower()
    assert "password" not in json.dumps(manifest).lower()


def test_backup_all_trigger_automatic(tmp_path):
    db = _make_db_file(tmp_path / "src.sqlite3")
    media = tmp_path / "media"
    media.mkdir()
    run = backup.backup_all(
        root=tmp_path / "backups", settings_dict=_sqlite_settings(db),
        media_root=media, trigger="automatic",
    )
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["type"] == "automatic"


def test_prune_keeps_last_n(tmp_path):
    root = tmp_path / "backups"
    for name in ["2026-01-01_00-00-00", "2026-02-01_00-00-00", "2026-03-01_00-00-00"]:
        (root / name).mkdir(parents=True)
    removed = backup.prune_old_runs(root, keep_last=2)
    assert len(removed) == 1
    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["2026-02-01_00-00-00", "2026-03-01_00-00-00"]


# --- Restore -----------------------------------------------------------------


def test_restore_media_round_trip(tmp_path):
    media = tmp_path / "media"
    (media / "sub").mkdir(parents=True)
    (media / "sub" / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    archive = backup.backup_media(tmp_path / "run", media_root=media)
    target = tmp_path / "restored"
    backup.restore_media(archive, media_root=target)
    assert (target / "sub" / "p.png").exists()


def test_restore_db_refuses_without_yes(tmp_path):
    src = _make_db_file(tmp_path / "b.sqlite3")
    with pytest.raises(CommandError, match="--yes"):
        call_command("restore_db", str(src))


def test_restore_media_refuses_without_yes(tmp_path):
    with pytest.raises(CommandError, match="--yes"):
        call_command("restore_media", str(tmp_path / "x.tar.gz"))


# --- ops_check ---------------------------------------------------------------


def test_ops_check_reports_media_and_backup(db, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.BACKUP_ROOT = tmp_path / "backups"
    results = checks.run_checks()
    names = {r.name for r in results}
    assert "MEDIA_ROOT" in names
    assert "BACKUP_ROOT" in names
    media = next(r for r in results if r.name == "MEDIA_ROOT")
    assert media.level == checks.OK  # tmp писабелен


def test_ops_check_flags_missing_pg_dump(db, settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.BACKUP_ROOT = tmp_path / "backups"
    monkeypatch.setattr(checks.shutil, "which", lambda _name: None)
    results = checks.run_checks(settings_dict=_postgres_settings())
    pg = next(r for r in results if r.name == "Клиенты PostgreSQL")
    assert pg.level == checks.FAIL
    assert checks.has_failures(results) is True


def test_ops_check_command_runs(db, settings, tmp_path):
    # На SQLite-тестах команда проходит (pg_dump не требуется).
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.BACKUP_ROOT = tmp_path / "backups"
    call_command("ops_check")


# --- Конфигурация проекта ----------------------------------------------------


def test_env_example_has_required_vars():
    text = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
    for key in ["DJANGO_SECRET_KEY", "DATABASE_URL", "POSTGRES_DB", "DJANGO_SUPERUSER_USERNAME"]:
        assert key in text


def test_gitignore_excludes_data():
    text = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")
    assert "mediafiles" in text
    assert "/backups/" in text


def test_compose_has_media_volume_and_backup_mount():
    text = (BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert "media:/app/mediafiles" in text
    assert "./backups:/app/backups" in text
    assert "pgdata" in text


def test_caddyfile_serves_media():
    text = (BASE_DIR / "docker" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "/media/*" in text


# --- Read-only относительно склада -------------------------------------------


def test_operations_do_not_touch_stock(db, settings, tmp_path):
    settings.BACKUP_ROOT = tmp_path / "backups"
    settings.MEDIA_ROOT = str(tmp_path / "media")
    mv_before = StockMovement.objects.count()
    bal_before = sorted(StockBalance.objects.values_list("id", "quantity_available"))
    call_command("ops_check")
    call_command("backup_media")
    assert StockMovement.objects.count() == mv_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_available")) == bal_before


def test_healthz_still_works(db, client):
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_no_pending_migrations(db):
    from io import StringIO

    out = StringIO()
    try:
        call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
    except SystemExit:
        pytest.fail(f"Есть несозданные миграции:\n{out.getvalue()}")
