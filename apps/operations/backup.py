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


# --- Клиенты PostgreSQL (явные версии, план 37 + hotfix 2) ---------------------

# Debian/PGDG кладёт клиентов по versioned-путям. Unversioned PATH — только
# запасной вариант для dev-окружений без пакетов Debian.
PG_BIN_TEMPLATE = "/usr/lib/postgresql/{version}/bin/{name}"
BACKUP_PG_VERSION = 16  # дампы делаем клиентом major-версии сервера (postgres:16)
RESTORE_PG_VERSION = 16  # восстановление начинаем клиентом сервера
RESTORE_FALLBACK_PG_VERSION = 17  # умеет читать архивы pg_dump 17 (формат 1.16)

# pg_restore 16 не читает custom-архивы pg_dump 17 (заголовок формата 1.16).
UNSUPPORTED_ARCHIVE_MARKER = "unsupported version (1.16) in file header"


def pg_binary(name: str, version: int | None = None) -> str | None:
    """Путь клиента нужной major-версии; иначе unversioned из PATH (dev)."""
    if version is not None:
        explicit = Path(PG_BIN_TEMPLATE.format(version=version, name=name))
        if explicit.exists():
            return str(explicit)
    return shutil.which(name)


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
        pg_dump = pg_binary("pg_dump", BACKUP_PG_VERSION)
        if pg_dump is None:
            raise OperationsError(
                "pg_dump недоступен. Установите postgresql-client-16 "
                "(или выполните бэкап из сервиса db)."
            )
        dest = dest_dir / "db.dump"
        cmd = [
            pg_dump, "-Fc", "-f", str(dest),
            "-h", str(s.get("HOST") or "localhost"),
            "-p", str(s.get("PORT") or "5432"),
            "-U", str(s.get("USER") or ""),
            str(s.get("NAME") or ""),
        ]
        _run(cmd, s.get("PASSWORD"))
        return dest

    raise OperationsError(f"Неизвестный движок БД: {engine}")


# Тип бэкапа (пишется в manifest["type"]). manual — ручной экспорт из UI/CLI.
BACKUP_TYPES = ("manual", "automatic", "pre_restore", "uploaded")


def backup_all(
    *, root=None, keep_last=None, settings_dict=None, media_root=None, trigger="manual"
) -> Path:
    """Полный бэкап: db + media + manifest.json в одном каталоге рана.

    `trigger` — источник бэкапа (пишется в manifest как поле `type`): manual (ручной,
    по умолчанию) / automatic (планировщик) / pre_restore (перед восстановлением) /
    uploaded (загруженный). Структура db/media не меняется.
    """
    s = settings_dict or connection.settings_dict
    run = new_run_dir(root)
    db_path = backup_db(run, settings_dict=s)
    media_path = backup_media(run, media_root=media_root)
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "type": trigger,
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

# Известная безвредная несовместимость: pg_dump 17 пишет в дамп
# "SET transaction_timeout = 0", а PostgreSQL 16 такого параметра не знает
# (инцидент 2026-07-02, план 37). Настройка не влияет на данные дампа.
_KNOWN_HARMLESS_RESTORE_ERROR = (
    'unrecognized configuration parameter "transaction_timeout"'
)


def pg_restore_error_filter(stderr: str) -> tuple[list[str], list[str]]:
    """Разделить stderr pg_restore на фатальные и известные безвредные ошибки.

    Возвращает (fatal, harmless). Толерантность СТРОГО к одному известному
    случаю: пропущенный `SET transaction_timeout` из дампа, сделанного клиентом
    новее сервера. ЛЮБАЯ другая ошибка pg_restore остаётся фатальной.
    """
    fatal, harmless = [], []
    for line in (stderr or "").splitlines():
        if "pg_restore: error:" not in line:
            continue
        if _KNOWN_HARMLESS_RESTORE_ERROR in line:
            harmless.append(line.strip())
        else:
            fatal.append(line.strip())
    return fatal, harmless


def restore_media(archive, *, media_root=None) -> None:
    """Распаковать `media.tar.gz` в media root (перезапись). Только из доверенного бэкапа."""
    archive = Path(archive)
    if not archive.exists():
        raise OperationsError(f"Архив media не найден: {archive}")
    media_root = Path(media_root) if media_root else Path(settings.MEDIA_ROOT)
    media_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(media_root, filter="data")  # data-фильтр против path traversal


def restore_db(source, *, settings_dict=None) -> list[str]:
    """Восстановить БД из дампа. Postgres → pg_restore --clean; SQLite → копия файла.

    Возвращает список предупреждений (пустой при чистом восстановлении).
    Единственная толерантность: известная безвредная ошибка про
    `transaction_timeout` из старых дампов (см. pg_restore_error_filter);
    в этом случае восстановление считается успешным с предупреждением.
    """
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
        return []

    if "postgresql" in engine:
        primary = pg_binary("pg_restore", RESTORE_PG_VERSION)
        if primary is None:
            raise OperationsError(
                "pg_restore недоступен. Установите postgresql-client-16."
            )

        def _cmd(binary: str) -> list:
            return [
                binary, "--clean", "--if-exists", "--no-owner",
                "-h", str(s.get("HOST") or "localhost"),
                "-p", str(s.get("PORT") or "5432"),
                "-U", str(s.get("USER") or ""),
                "-d", str(s.get("NAME") or ""),
                str(source),
            ]

        env = {**os.environ}
        if s.get("PASSWORD"):
            env["PGPASSWORD"] = str(s["PASSWORD"])

        warnings: list[str] = []
        result = subprocess.run(_cmd(primary), env=env, capture_output=True, text=True)
        if result.returncode != 0 and UNSUPPORTED_ARCHIVE_MARKER in (result.stderr or ""):
            # Старый архив pg_dump 17 (формат 1.16): pg_restore 16 его не читает.
            # Единственный fallback: pg_restore 17 (стоит в образе рядом с 16).
            fallback = pg_binary("pg_restore", RESTORE_FALLBACK_PG_VERSION)
            if fallback is None or fallback == primary:
                raise OperationsError(
                    "Архив создан pg_dump 17 (формат 1.16): pg_restore 16 его не "
                    "читает. Установите postgresql-client-17 (в актуальном "
                    "web-образе он есть) и повторите."
                )
            warnings.append(
                "старый архив pg_dump 17 (формат 1.16): восстановление выполнено "
                "клиентом pg_restore 17"
            )
            result = subprocess.run(
                _cmd(fallback), env=env, capture_output=True, text=True
            )
        if result.returncode == 0:
            return warnings
        fatal, harmless = pg_restore_error_filter(result.stderr)
        if fatal or not harmless:
            raise OperationsError(
                f"pg_restore завершился с ошибкой: {result.stderr or result.returncode}"
            )
        warnings.append(
            "пропущен SET transaction_timeout: дамп сделан клиентом PostgreSQL "
            "новее сервера; на данные не влияет (план 37)"
        )
        return warnings

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
