"""v1.1.7 — Backup UI (только owner/admin, read-only + создание локального бэкапа).

Тонкая обёртка над `apps/operations/backup.py`. Скачивание — только файлы из
конкретного backup-run (защита от path traversal). Складскую логику не трогает.

Layer 30: добавлено аварийное веб-восстановление, но НЕ для всех админов:
только allowlist-владелец при включённом флаге DENSTOCK_ENABLE_WEB_RESTORE
(restore.can_web_restore), с обязательной проверкой бэкапа, фразой
«ПОДТВЕРЖДАЮ», checkbox риска и pre-restore бэкапом. Для остальных
пользователей web-restore по-прежнему отсутствует.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import backup, restore
from .models import RestoreJob

# Единственные файлы, которые вообще можно отдать из backup-run.
ALLOWED_FILES = ("manifest.json", "db.dump", "db.sqlite3", "media.tar.gz")

# Читабельные подписи и pill-стиль для manifest["type"].
TYPE_LABELS = {
    "manual": "Ручной",
    "automatic": "Автоматический",
    "pre_restore": "Перед восстановлением",
    "uploaded": "Загруженный",
}
TYPE_PILLS = {
    "manual": "pill--info",
    "automatic": "pill--success",
    "pre_restore": "pill--warning",
    "uploaded": "pill--muted",
}


def _type_label(manifest) -> str:
    if not manifest or not manifest.get("type"):
        return "Legacy"
    return TYPE_LABELS.get(manifest["type"], "Неизвестный тип")


def _type_pill(manifest) -> str:
    if not manifest or not manifest.get("type"):
        return "pill--muted"
    return TYPE_PILLS.get(manifest["type"], "pill--muted")


def _require_admin(request) -> None:
    """Раздел только для owner/admin (superuser или роль «Администратор»)."""
    if not request.user.is_admin:
        raise PermissionDenied


def _safe_run_dir(run_id: str):
    """Каталог одного backup-run строго внутри BACKUP_ROOT (без path traversal)."""
    root = backup.backup_root().resolve()
    candidate = (root / run_id).resolve()
    if candidate.parent != root or not candidate.is_dir():
        raise Http404("Бэкап не найден.")
    return candidate


def _read_manifest(run_dir):
    path = run_dir / "manifest.json"
    if not path.exists():
        return None, "manifest.json отсутствует"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"manifest повреждён: {exc}"


def _run_info(run_dir) -> dict:
    manifest, manifest_error = _read_manifest(run_dir)
    files = [
        {"name": name, "size": (run_dir / name).stat().st_size}
        for name in ALLOWED_FILES
        if (run_dir / name).exists()
    ]
    return {
        "run_id": run_dir.name,
        "files": files,
        "has_manifest": manifest is not None,
        "manifest": manifest or {},
        "manifest_error": manifest_error,
        "type_label": _type_label(manifest),
        "type_pill": _type_pill(manifest),
    }


def _list_runs() -> list:
    root = backup.backup_root()
    if not root.exists():
        return []
    runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    return [_run_info(p) for p in runs]


def _offsite_status():
    path = backup.backup_root() / "offsite_status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"error": "status-файл повреждён"}


@login_required
def backups_list(request):
    _require_admin(request)
    runs = _list_runs()
    can_restore = restore.can_web_restore(request.user)
    context = {
        "runs": runs,
        "last_run": runs[0] if runs else None,
        "offsite": _offsite_status(),
        "backup_root": str(backup.backup_root()),
        "can_restore": can_restore,
    }
    if can_restore:
        # Данные ТОЛЬКО для allowlist-владельца: список для восстановления
        # (с размером и меткой «старый») и журнал прошлых восстановлений.
        for run in runs:
            run["total_size"] = sum(f["size"] for f in run["files"])
            run["is_old"] = restore.restore_age_warning(run["manifest"])
            run["has_db"] = any(
                f["name"] in ("db.dump", "db.sqlite3") for f in run["files"]
            )
            run["has_media"] = any(f["name"] == "media.tar.gz" for f in run["files"])
        context["restore_jobs"] = RestoreJob.objects.all()[:10]
    return render(request, "operations/backups.html", context)


@login_required
@require_POST
def backup_create(request):
    _require_admin(request)
    try:
        run = backup.backup_all(trigger="manual")  # ручной экспорт; НЕ restore
    except backup.OperationsError as exc:
        messages.error(request, f"Не удалось создать бэкап: {exc}")
    else:
        messages.success(request, f"Полный бэкап создан: {run.name}")
    return redirect("operations:backups")


@login_required
def backup_manifest(request, run_id):
    _require_admin(request)
    run_dir = _safe_run_dir(run_id)
    manifest, error = _read_manifest(run_dir)
    file_status = [{"name": name, "present": (run_dir / name).exists()} for name in ALLOWED_FILES]
    return render(
        request,
        "operations/backup_manifest.html",
        {
            "run_id": run_id,
            "manifest": manifest,
            "error": error,
            "file_status": file_status,
            "type_label": _type_label(manifest),
            "type_pill": _type_pill(manifest),
        },
    )


# --- Layer 30: аварийное восстановление (только allowlist-владелец) -----------


def _require_restore_owner(request) -> None:
    """Флаг + admin + allowlist. Для всех остальных раздел не существует."""
    _require_admin(request)
    if not restore.can_web_restore(request.user):
        raise PermissionDenied


@login_required
def backup_restore(request, run_id):
    """GET: проверка бэкапа + экран подтверждения. POST: запуск восстановления.

    Restore выполняется только при: POST + CSRF + успешной проверке + фразе
    «ПОДТВЕРЖДАЮ» + checkbox риска. GET никогда ничего не запускает.
    """
    _require_restore_owner(request)
    _safe_run_dir(run_id)  # 404 для несуществующего/подделанного run_id
    report = restore.verify_backup(run_id)

    if request.method == "POST":
        phrase = (request.POST.get("confirm_phrase") or "").strip()
        accepted = request.POST.get("accept_risk") == "on"
        if not report.ok:
            messages.error(request, "Бэкап не прошёл проверку: восстановление запрещено.")
        elif phrase != restore.CONFIRM_PHRASE:
            messages.error(
                request,
                "Фраза подтверждения неверна: введите слово ПОДТВЕРЖДАЮ (заглавными).",
            )
        elif not accepted:
            messages.error(
                request, "Отметьте, что понимаете: текущие данные будут перезаписаны."
            )
        else:
            job = restore.run_web_restore(run_id, user=request.user)
            # Результат рендерим сразу: redirect бесполезен, сессии в базе
            # только что перезаписаны восстановлением.
            return render(
                request, "operations/restore_result.html", {"job": job}
            )

    return render(
        request,
        "operations/restore_confirm.html",
        {
            "run_id": run_id,
            "report": report,
            "confirm_phrase": restore.CONFIRM_PHRASE,
        },
    )


@login_required
def backup_download(request, run_id, filename):
    _require_admin(request)
    run_dir = _safe_run_dir(run_id)
    if filename not in ALLOWED_FILES:
        raise Http404("Недопустимый файл бэкапа.")
    path = (run_dir / filename).resolve()
    if path.parent != run_dir or not path.is_file():
        raise Http404("Файл не найден.")
    return FileResponse(path.open("rb"), as_attachment=True, filename=filename)
