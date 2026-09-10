"""Unauthenticated runtime probes for the future public catalog.

There is deliberately no browse/search UI in Stage 3.  These endpoints prove
the separate runtime boundary without exposing an internal screen or DTO.
"""

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def public_root(request):
    return JsonResponse({"service": "public-catalog", "status": "ok"})


@require_GET
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - readiness must fail closed
        return JsonResponse({"status": "down", "db": "down"}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})
