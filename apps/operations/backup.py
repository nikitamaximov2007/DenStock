"""Слой 25 — резервное копирование и восстановление (тестируемые функции).

Эксплуатационный слой: НЕ трогает складскую логику. Функции принимают пути/параметры,
поэтому тестируются на временных каталогах без боевой БД. Управляющие команды
(`apps/operations/management/commands/*`) — тонкие обёртки над этими функциями.

Безопасность: пароль БД передаётся в `pg_dump`/`pg_restore` через переменную окружения
`PGPASSWORD` (не в argv), в вывод/манифест секреты не пишутся.
"""
import json
import os
import shutil
import subprocess
import tarfile
from datetime import datetime
from importlib import metadata
from pathlib import Path

from django.conf import settings
from django.db import connection


class OperationsError(Exception):
    """Понятная эксплуатационная ошибка (команды переводят её в CommandError)."""


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def backup_root() -> Path:
    return Path(getattr(settings, "BACKUP_ROOT", Path(settings.BASE_DIR) / "backups"))


def new_run_dir(root=None) -> Path:
    base = Path(root) if root else backup_root()
    run = base / timestamp()
    run.mkdir(parents=True, exist_ok=True)
    return run


def _engine_name(engine: str) -> str:
    if "postgresql" in engine:
        return "postgresql"
    if "sqlite" in engine:
        return "sqlite"
    return engine


# --- Backup ------------------------------------------------------------------


def backup_media(dest_dir, *, media_root=None) -> Path | None:
    """Заархивировать media в `<dest_dir>/media.tar.gz`. None — если файлов нет."""
    media_root = Path(media_root) if media_root else Path(settings.MEDIA_ROOT)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    has_files = media_root.exists() and any(p.is_file() for p in media_root.rglob("*"))
    if not has_files:
        return None
    archive = dest_dir / "media.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(str(media_root), arcname=".")
    return archive


def backup_db(dest_dir, *, settings_dict=None) -> Path:
    """Сделать дамп БД в `dest_dir`. Postgres → pg_dump -Fc; SQLite → копия файла."""
    s = settings_dict or connection.settings_dict
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    engine = s["ENGINE"]

    if "sqlite" in engine:
        name = s.get("NAME")
        if not name or str(name) == ":memory:":
            raise OperationsError("БД SQLite в памяти — нечего копировать.")
        src = Path(name)
        if not src.exists():
            raise OperationsError(f"Файл БД не найден: {src}")
        dest = dest_dir / "db.sqlite3"
        shutil.copy2(src, dest)
        return dest

    if "postgresql" in engine:
        if shutil.which("pg_dump") is None:
            raise OperationsError(
                "pg_dump недоступен. Установите postgresql-client "
                "(или выполните бэкап из сервиса db)."
            )
        dest = dest_dir / "db.dump"
        cmd = [
            "pg_dump", "-Fc", "-f", str(dest),
            "-h", str(s.get("HOST") or "localhost"),
            "-p", str(s.get("PORT") or "5432"),
            "-U", str(s.get("USER") or ""),
            str(s.get("NAME") or ""),
        ]
        _run(cmd, s.get("PASSWORD"))
        return dest

    raise OperationsError(f"Неизвестный движок БД: {engine}")


def backup_all(*, root=None, keep_last=None, settings_dict=None, media_root=None) -> Path:
    """Полный бэкап: db + media + manifest.json в одном каталоге рана."""
    s = settings_dict or connection.settings_dict
    run = new_run_dir(root)
    db_path = backup_db(run, settings_dict=s)
    media_path = backup_media(run, media_root=media_root)
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "engine": _engine_name(s["ENGINE"]),
        "db_file": db_path.name if db_path else None,
        "media_file": media_path.name if media_path else None,
        "version": _project_version(),
        "git_commit": _git_commit(),
    }
    (run / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if keep_last:
        prune_old_runs(run.parent, keep_last)
    return run


def prune_old_runs(root, keep_last: int) -> list[Path]:
    """Оставить `keep_last` свежих каталогов бэкапов, остальные удалить. Возвращает удалённые."""
    root = Path(root)
    runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    to_remove = runs[:-keep_last] if keep_last > 0 else []
    for path in to_remove:
        shutil.rmtree(path, ignore_errors=True)
    return to_remove


# --- Restore -----------------------------------------------------------------


def restore_media(archive, *, media_root=None) -> None:
    """Распаковать `media.tar.gz` в media root (перезапись). Только из доверенного бэкапа."""
    archive = Path(archive)
    if not archive.exists():
        raise OperationsError(f"Архив media не найден: {archive}")
    media_root = Path(media_root) if media_root else Path(settings.MEDIA_ROOT)
    media_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(media_root, filter="data")  # data-фильтр против path traversal


def restore_db(source, *, settings_dict=None) -> None:
    """Восстановить БД из дампа. Postgres → pg_restore --clean; SQLite → копия файла."""
    s = settings_dict or connection.settings_dict
    source = Path(source)
    if not source.exists():
        raise OperationsError(f"Файл бэкапа не найден: {source}")
    engine = s["ENGINE"]

    if "sqlite" in engine:
        name = s.get("NAME")
        if not name or str(name) == ":memory:":
            raise OperationsError("Невозможно восстановить в SQLite :memory:.")
        connection.close()
        shutil.copy2(source, name)
        return

    if "postgresql" in engine:
        if shutil.which("pg_restore") is None:
            raise OperationsError("pg_restore недоступен. Установите postgresql-client.")
        cmd = [
            "pg_restore", "--clean", "--if-exists", "--no-owner",
            "-h", str(s.get("HOST") or "localhost"),
            "-p", str(s.get("PORT") or "5432"),
            "-U", str(s.get("USER") or ""),
            "-d", str(s.get("NAME") or ""),
            str(source),
        ]
        _run(cmd, s.get("PASSWORD"))
        return

    raise OperationsError(f"Неизвестный движок БД: {engine}")


# --- helpers -----------------------------------------------------------------


def _run(cmd: list, password) -> None:
    """Запустить клиент Postgres; пароль — через PGPASSWORD (не в argv, не в логах)."""
    env = {**os.environ}
    if password:
        env["PGPASSWORD"] = str(password)
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        # Печатаем stderr команды, но не пароль (его в argv нет).
        raise OperationsError(f"{cmd[0]} завершился с ошибкой: {exc.stderr or exc}") from exc


def _project_version() -> str | None:
    try:
        return metadata.version("denstock")
    except Exception:  # noqa: BLE001 — версия необязательна для бэкапа
        return None


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(settings.BASE_DIR), capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — git может отсутствовать
        return None
