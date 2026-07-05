"""Layer 30 — защищённое аварийное восстановление из веб-интерфейса.

Многоступенчатая защита (все проверки обязательны):
1. feature flag DENSTOCK_ENABLE_WEB_RESTORE (по умолчанию выключен);
2. allowlist владельца (email/username из настроек) + admin/superuser;
3. verify_backup перед restore (read-only проверка целостности);
4. фраза «ПОДТВЕРЖДАЮ» + checkbox понимания риска (проверяет view, POST+CSRF);
5. обязательный pre-restore бэкап текущей базы и media: не создался — restore
   не выполняется вообще.

Выполнение синхронное и осознанно НЕ в транзакции Django: восстановление
PostgreSQL делает существующий pg_restore-путь (apps/operations/backup.py),
тот же, что в CLI-runbook, где web-контейнер продолжает работать. Оценка
риска: база DenStock маленькая (минуты недопустимого простоя не возникают),
перед restore соединения Django закрываются, а откат при неудаче — вручную
из pre-restore бэкапа (путь пишется в журнал до начала restore). Журнал
дублируется в файл `<BACKUP_ROOT>/restore.log`, потому что строки БД
перезаписываются самим восстановлением.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management import call_command
from django.db import connection, connections
from django.utils import timezone

from . import backup
from .models import RestoreJob

CONFIRM_PHRASE = "ПОДТВЕРЖДАЮ"
OLD_BACKUP_DAYS = 30  # старше — предупреждение «бэкап старый»


# --- Кто вообще видит restore ----------------------------------------------------


def can_web_restore(user) -> bool:
    """Restore-доступ: флаг включён + admin + email/username в allowlist."""
    if not getattr(settings, "DENSTOCK_ENABLE_WEB_RESTORE", False):
        return False
    if not getattr(user, "is_authenticated", False) or not user.is_admin:
        return False
    allowed_emails = [
        e.strip().lower() for e in settings.DENSTOCK_RESTORE_ALLOWED_EMAILS if e.strip()
    ]
    allowed_usernames = [
        u.strip() for u in settings.DENSTOCK_RESTORE_ALLOWED_USERNAMES if u.strip()
    ]
    email_ok = bool(user.email) and user.email.strip().lower() in allowed_emails
    username_ok = user.username in allowed_usernames
    return email_ok or username_ok


# --- Проверка бэкапа (read-only) ---------------------------------------------------


@dataclass
class VerifyReport:
    run_id: str
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    checks: list = field(default_factory=list)  # [(подпись, "ok"/"warn"/"fail")]
    manifest: dict = field(default_factory=dict)
    db_file: str = ""
    media_file: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def check(self, label: str, passed: bool, *, error: str = "", warn: str = "") -> bool:
        if passed:
            self.checks.append((label, "ok"))
        elif warn:
            self.checks.append((f"{label}: {warn}", "warn"))
            self.warnings.append(f"{label}: {warn}")
        else:
            self.checks.append((f"{label}: {error}" if error else label, "fail"))
            self.errors.append(error or label)
        return passed


def _safe_run_dir(run_id: str):
    """Каталог бэкапа строго внутри BACKUP_ROOT (никакого path traversal)."""
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    root = backup.backup_root().resolve()
    candidate = (root / run_id).resolve()
    if candidate.parent != root or not candidate.is_dir():
        return None
    return candidate


def verify_backup(run_id: str) -> VerifyReport:
    """Проверить бэкап перед восстановлением. Ничего не меняет."""
    report = VerifyReport(run_id=run_id)
    run_dir = _safe_run_dir(run_id)
    if not report.check(
        "Бэкап внутри каталога backups", run_dir is not None,
        error="бэкап не найден или путь вне каталога backups",
    ):
        return report

    manifest_path = run_dir / "manifest.json"
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = None
    if not report.check(
        "manifest.json читается", manifest is not None,
        error="manifest.json отсутствует или повреждён",
    ):
        return report
    report.manifest = manifest

    known_keys = {"created_at", "engine", "db_file"}
    report.check(
        "Бэкап принадлежит DenStock", known_keys <= set(manifest),
        error="в manifest нет обязательных полей DenStock (created_at/engine/db_file)",
    )

    db_name = manifest.get("db_file") or ""
    db_path = run_dir / db_name if db_name else None
    if report.check(
        "Файл базы существует и не пустой",
        bool(db_path) and db_path.exists() and db_path.stat().st_size > 0,
        error=f"файл базы «{db_name or '?'}» отсутствует или пуст",
    ):
        report.db_file = db_name

    # Известная несовместимость версий: pg_dump 17 пишет SET transaction_timeout,
    # PostgreSQL 16 его не знает. Restore обрабатывает это автоматически, но
    # честно предупреждаем заранее (сканируем начало дампа: SET-команды в TOC).
    if report.db_file and report.db_file.endswith(".dump"):
        try:
            with (run_dir / report.db_file).open("rb") as fh:
                head = fh.read(131072)
        except OSError:
            head = b""
        if b"transaction_timeout" in head:
            report.check(
                "Совместимость дампа", False,
                warn="дамп содержит SET transaction_timeout (создан клиентом новее "
                     "сервера); restore пропустит его автоматически",
            )

    media_name = manifest.get("media_file")
    if media_name:
        media_path = run_dir / media_name
        if report.check(
            "Архив media существует и не пустой",
            media_path.exists() and media_path.stat().st_size > 0,
            error=f"архив media «{media_name}» указан в manifest, но отсутствует или пуст",
        ):
            report.media_file = media_name
    else:
        report.checks.append(("Media в бэкапе нет (по manifest — корректно)", "ok"))

    # Контрольные суммы: проверяем, только если manifest их содержит.
    checksums = manifest.get("sha256") or {}
    if checksums:
        import hashlib

        for name, expected in checksums.items():
            path = run_dir / name
            actual = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
            )
            report.check(
                f"Контрольная сумма {name}", actual == expected,
                error=f"контрольная сумма {name} не совпадает",
            )
    else:
        report.checks.append(("Контрольных сумм в manifest нет (не проверялись)", "warn"))

    engine_now = "postgresql" if "postgresql" in connection.settings_dict["ENGINE"] else "sqlite"
    report.check(
        "Движок бэкапа совпадает с текущей БД",
        manifest.get("engine") == engine_now,
        error=f"бэкап для «{manifest.get('engine')}», а текущая БД {engine_now}",
    )

    version_now = backup._project_version()
    if manifest.get("version") and version_now and manifest["version"] != version_now:
        report.check(
            "Версия приложения", False,
            warn=f"бэкап от версии {manifest['version']}, сейчас {version_now}",
        )

    if manifest.get("type") == "pre_restore":
        report.check(
            "Тип бэкапа", False,
            warn="это pre-restore бэкап (страховочная копия перед прошлым восстановлением)",
        )

    created = manifest.get("created_at") or ""
    try:
        created_dt = datetime.fromisoformat(created)
    except ValueError:
        created_dt = None
    if created_dt and datetime.now() - created_dt > timedelta(days=OLD_BACKUP_DAYS):
        report.check(
            "Свежесть бэкапа", False,
            warn=f"бэкапу больше {OLD_BACKUP_DAYS} дней ({created[:10]})",
        )
    return report


# --- Выполнение (синхронно, вне транзакции Django) ---------------------------------


def _file_log(lines: list[str]) -> None:
    """Надёжный след в файле: переживает перезапись БД."""
    try:
        path = backup.backup_root() / "restore.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n---\n")
    except OSError:
        pass  # файл-лог вспомогательный: не роняем restore из-за него


def _write_job(user, run_id, status, *, pre_run_id="", log_lines, error="") -> RestoreJob:
    job = RestoreJob(
        run_id=run_id,
        status=status,
        started_by_username=getattr(user, "username", "") or "",
        pre_restore_run_id=pre_run_id,
        log="\n".join(log_lines),
        error=error,
        finished_at=timezone.now(),
    )
    # FK можно ставить только если пользователь существует в ТЕКУЩЕЙ базе
    # (после восстановления другой базы его может не быть).
    try:
        if user is not None and type(user).objects.filter(pk=user.pk).exists():
            job.started_by = user
    except Exception:  # noqa: BLE001 — журнал важнее ссылки
        pass
    job.save()
    return job


def run_web_restore(run_id: str, *, user) -> RestoreJob:
    """Полный цикл: verify -> pre-restore бэкап -> restore db+media -> migrate.

    Строка RestoreJob пишется в конце (успех — в восстановленную базу после
    migrate; ошибка до restore — в текущую нетронутую базу). Файл restore.log
    ведётся на каждом шаге.
    """
    log = [f"[{timezone.now():%Y-%m-%d %H:%M:%S}] restore {run_id}: "
           f"запустил {getattr(user, 'username', '?')}"]

    log.append("шаг 1/4: проверка бэкапа (verifying)")
    report = verify_backup(run_id)
    if not report.ok:
        error = "; ".join(report.errors)
        log.append(f"проверка провалена: {error}")
        _file_log(log)
        return _write_job(user, run_id, RestoreJob.Status.FAILED,
                          log_lines=log, error=f"Проверка бэкапа: {error}")

    log.append("шаг 2/4: pre-restore бэкап текущей базы и media (pre_backup)")
    try:
        pre_run = backup.backup_all(trigger="pre_restore")
    except backup.OperationsError as exc:
        log.append(f"pre-restore бэкап НЕ создан: {exc}. Restore отменён.")
        _file_log(log)
        return _write_job(user, run_id, RestoreJob.Status.FAILED,
                          log_lines=log, error=f"Pre-restore бэкап не создан: {exc}")
    pre_run_id = pre_run.name
    log.append(f"pre-restore бэкап создан: {pre_run_id}")

    run_dir = _safe_run_dir(run_id)
    db_path = run_dir / report.db_file
    media_path = (run_dir / report.media_file) if report.media_file else None

    log.append("шаг 3/4: восстановление базы и media (restoring)")
    _file_log(log)  # фиксируем след ДО перезаписи базы
    try:
        connections.close_all()  # не держим соединения во время pg_restore
        for warning in backup.restore_db(db_path) or []:
            log.append(f"предупреждение: {warning}")
        if media_path is not None:
            backup.restore_media(media_path)
        log.append("шаг 4/4: применение миграций (migrated)")
        call_command("migrate", interactive=False, verbosity=0)
    except Exception as exc:  # noqa: BLE001 — причина уходит в журнал, не глотается
        log.append(f"ОШИБКА восстановления: {exc}")
        log.append(f"откат: восстановите pre-restore бэкап {pre_run_id} по runbook")
        _file_log(log)
        return _write_job(user, run_id, RestoreJob.Status.FAILED,
                          pre_run_id=pre_run_id, log_lines=log,
                          error=f"{exc} (откат: pre-restore бэкап {pre_run_id})")

    log.append("восстановление завершено (completed)")
    _file_log(log)
    return _write_job(user, run_id, RestoreJob.Status.COMPLETED,
                      pre_run_id=pre_run_id, log_lines=log)


def restore_age_warning(manifest: dict) -> bool:
    """Старый ли бэкап (для пометки в списке выбора)."""
    created = (manifest or {}).get("created_at") or ""
    try:
        created_dt = datetime.fromisoformat(created)
    except ValueError:
        return False
    return datetime.now() - created_dt > timedelta(days=OLD_BACKUP_DAYS)
