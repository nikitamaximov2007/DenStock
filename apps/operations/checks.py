"""Слой 25 — проверка готовности к эксплуатации (`ops_check`).

Отдельно от `/healthz/` (который остаётся лёгким app+DB): здесь — проверки, которые
делать на каждый HTTP-healthcheck не нужно (запись на диск, наличие клиентов БД).
"""
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import connection

from . import backup

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    level: str  # ok | warn | fail
    message: str


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ops_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001 — нам важен факт (не)писабельности
        return False


def run_checks(settings_dict=None) -> list[CheckResult]:
    s = settings_dict or connection.settings_dict
    results: list[CheckResult] = []

    # 1. База данных.
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        results.append(CheckResult("База данных", OK, "доступна"))
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("База данных", FAIL, f"недоступна: {exc}"))

    # 2. MEDIA_ROOT.
    media_root = Path(settings.MEDIA_ROOT)
    if _writable(media_root):
        results.append(CheckResult("MEDIA_ROOT", OK, f"{media_root} доступен на запись"))
    else:
        results.append(CheckResult("MEDIA_ROOT", FAIL, f"{media_root} недоступен на запись"))

    # 3. BACKUP_ROOT.
    backup_dir = backup.backup_root()
    if _writable(backup_dir):
        results.append(CheckResult("BACKUP_ROOT", OK, f"{backup_dir} доступен на запись"))
    else:
        results.append(CheckResult("BACKUP_ROOT", FAIL, f"{backup_dir} недоступен на запись"))

    # 4. Клиенты БД (только для PostgreSQL).
    if "postgresql" in s["ENGINE"]:
        missing = [t for t in ("pg_dump", "pg_restore") if shutil.which(t) is None]
        if missing:
            results.append(
                CheckResult("Клиенты PostgreSQL", FAIL,
                            f"не найдены: {', '.join(missing)} — установите postgresql-client")
            )
        else:
            results.append(CheckResult("Клиенты PostgreSQL", OK, "pg_dump/pg_restore доступны"))
    else:
        results.append(CheckResult("Клиенты PostgreSQL", OK, "SQLite — pg_dump не требуется"))

    # 5. Настройки media.
    if settings.MEDIA_URL and settings.MEDIA_ROOT:
        results.append(CheckResult("Настройки media", OK, f"MEDIA_URL={settings.MEDIA_URL}"))
    else:
        results.append(CheckResult("Настройки media", FAIL, "MEDIA_URL/MEDIA_ROOT не заданы"))

    # 6. Прод-настройки (мягкие предупреждения).
    if settings.DEBUG:
        results.append(CheckResult("DEBUG", WARN, "DEBUG=True — только для разработки"))
    else:
        results.append(CheckResult("DEBUG", OK, "DEBUG=False"))

    if settings.SECRET_KEY and "dev-insecure" not in settings.SECRET_KEY:
        results.append(CheckResult("SECRET_KEY", OK, "задан несдефолтный ключ"))
    else:
        results.append(CheckResult("SECRET_KEY", WARN, "небезопасный ключ по умолчанию"))

    hosts = list(settings.ALLOWED_HOSTS)
    if hosts and hosts != ["*"]:
        results.append(CheckResult("ALLOWED_HOSTS", OK, ", ".join(hosts)))
    else:
        results.append(CheckResult("ALLOWED_HOSTS", WARN, f"широкое значение: {hosts}"))

    return results


def has_failures(results: list[CheckResult]) -> bool:
    return any(r.level == FAIL for r in results)
