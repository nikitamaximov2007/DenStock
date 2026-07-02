"""v1.1.7 — Backup UI (только owner/admin, read-only + создание локального бэкапа).

Тонкая обёртка над `apps/operations/backup.py`. НАМЕРЕННО без web-restore: restore
остаётся CLI-операцией под `--yes`. Скачивание — только файлы из конкретного backup-run
(защита от path traversal). Складскую логику не трогает.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import backup

# Единственные файлы, которые вообще можно отдать из backup-run.
ALLOWED_FILES = ("manifest.json", "db.dump", "db.sqlite3", "media.tar.gz")


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
    return render(
        request,
        "operations/backups.html",
        {
            "runs": runs,
            "last_run": runs[0] if runs else None,
            "offsite": _offsite_status(),
            "backup_root": str(backup.backup_root()),
        },
    )


@login_required
@require_POST
def backup_create(request):
    _require_admin(request)
    try:
        run = backup.backup_all()  # та же логика, что CLI backup_all; НЕ restore
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
        {"run_id": run_id, "manifest": manifest, "error": error, "file_status": file_status},
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
