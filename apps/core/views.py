from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import UnresolvedScan
from .scanner import resolve_scan


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Рабочий скелет главной панели (наполняется на последующих слоях)."""
    return render(request, "core/dashboard.html")


# --- Слой 11: единый резолв сканера ------------------------------------------

_EMPTY_PAYLOAD = {
    "found": False, "status": "error", "type": None, "id": None,
    "label": "", "url": None, "message": "Пустой код.", "candidates": [],
}


def _record_unresolved(request: HttpRequest, code: str) -> None:
    """Журналировать нераспознанный скан. Анти-спам: тот же код тем же
    пользователем в пределах ~5 с новой строки не плодит."""
    recent = timezone.now() - timedelta(seconds=5)
    user = request.user if request.user.is_authenticated else None
    dup = UnresolvedScan.objects.filter(
        raw_value=code, user=user, created_at__gte=recent
    ).exists()
    if dup:
        return
    UnresolvedScan.objects.create(
        raw_value=code, user=user, context=request.POST.get("context", "")[:60]
    )


@login_required
@require_POST
def scanner_resolve(request: HttpRequest) -> JsonResponse:
    """Endpoint резолва: возвращает JSON-локатор. Только распознаёт, не действует.

    На реальном unknown пишет `UnresolvedScan` (сам резолвер — чистый).
    """
    code = (request.POST.get("code") or "").strip()
    if not code:
        return JsonResponse(_EMPTY_PAYLOAD, status=400)
    result = resolve_scan(code, user=request.user)
    if result.status == "unknown":
        _record_unresolved(request, code)
    return JsonResponse(result.to_dict())


@login_required
def scanner_page(request: HttpRequest) -> HttpResponse:
    """Страница «Сканер» (4.5). No-JS fallback: POST резолвится сервером."""
    result = None
    code = ""
    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        if code:
            result = resolve_scan(code, user=request.user)
            if result.status == "unknown":
                _record_unresolved(request, code)
    return render(request, "core/scanner.html", {"result": result, "code": code})


@login_required
def unresolved_list(request: HttpRequest) -> HttpResponse:
    """История нераспознанных сканов — только Админ/Руководитель."""
    if not (request.user.is_admin or request.user.is_manager):
        raise PermissionDenied
    scans = UnresolvedScan.objects.select_related("user", "resolved_part")[:200]
    return render(request, "core/unresolved_list.html", {"scans": scans})


def healthz(request: HttpRequest) -> JsonResponse:
    """Проверка доступности приложения и (lightweight) базы данных.

    Приложение отвечает → status=ok. Доступность БД проверяется простым
    запросом; при ошибке возвращается 503, чтобы Docker/мониторинг это видел.
    """
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 — для healthcheck достаточно факта недоступности
        db_ok = False

    payload = {"status": "ok", "db": "ok" if db_ok else "down"}
    return JsonResponse(payload, status=200 if db_ok else 503)
