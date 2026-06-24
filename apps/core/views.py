from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Рабочий скелет главной панели (наполняется на последующих слоях)."""
    return render(request, "core/dashboard.html")


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
