"""Слой 21 — экраны отчётов. View только вызывает сервисы и рендерит (read-only).

Денежные блоки скрываются в шаблоне по `can_view_purchase_cost` (`show_costs`);
доступ к разделу — по `can_view_reports`.
"""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from .services import (
    get_dashboard_report,
    get_low_stock_report,
    get_stock_report,
    resolve_period,
)

_PRESETS = [("today", "Сегодня"), ("7", "7 дней"), ("30", "30 дней"), ("month", "Месяц")]


def _require_reports(request) -> None:
    if not request.user.can_view_reports:
        raise PermissionDenied


@login_required
def reports_dashboard(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    report = get_dashboard_report(period)
    return render(
        request,
        "reports/dashboard.html",
        {
            "report": report,
            "period": period,
            "presets": _PRESETS,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def reports_stock(request):
    _require_reports(request)
    return render(
        request,
        "reports/stock.html",
        {
            "stock": get_stock_report(),
            "low_stock": get_low_stock_report(),
            "show_costs": request.user.can_view_purchase_cost,
        },
    )
