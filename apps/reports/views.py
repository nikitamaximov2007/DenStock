"""Слой 21/22 — экраны и CSV-экспорт отчётов. View только вызывает сервисы и
рендерит/отдаёт файл (read-only).

Денежные блоки скрываются в шаблоне по `can_view_purchase_cost` (`show_costs`);
доступ к разделу — по `can_view_reports`. Экспорт (Слой 22) использует ТЕ ЖЕ
сервисы и право, что UI; финансовые колонки пишутся только при purchase_cost.
"""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from . import exporters
from .services import (
    get_dashboard_report,
    get_low_stock_report,
    get_repairs_report,
    get_returns_report,
    get_sales_report,
    get_stock_report,
    get_stocktaking_report,
    get_writeoffs_report,
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
    period_qs = f"date_from={period.date_from:%Y-%m-%d}&date_to={period.date_to:%Y-%m-%d}"
    return render(
        request,
        "reports/dashboard.html",
        {
            "report": report,
            "period": period,
            "period_qs": period_qs,  # для ссылок «CSV» с тем же периодом
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


# --- Слой 22: CSV-экспорт (те же сервисы/право/гейт финансов, что UI) ---------


@login_required
def export_sales(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    header, rows = exporters.sales_rows(
        get_sales_report(period), period, include_costs=request.user.can_view_purchase_cost
    )
    return exporters.csv_response(exporters.export_filename("sales", period), header, rows)


@login_required
def export_returns(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    header, rows = exporters.returns_rows(
        get_returns_report(period), period, include_costs=request.user.can_view_purchase_cost
    )
    return exporters.csv_response(exporters.export_filename("returns", period), header, rows)


@login_required
def export_repairs(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    header, rows = exporters.repairs_rows(
        get_repairs_report(period), period, include_costs=request.user.can_view_purchase_cost
    )
    return exporters.csv_response(exporters.export_filename("repairs", period), header, rows)


@login_required
def export_writeoffs(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    header, rows = exporters.writeoffs_rows(
        get_writeoffs_report(period), period, include_costs=request.user.can_view_purchase_cost
    )
    return exporters.csv_response(exporters.export_filename("writeoffs", period), header, rows)


@login_required
def export_stocktaking(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    header, rows = exporters.stocktaking_rows(
        get_stocktaking_report(period), period, include_costs=request.user.can_view_purchase_cost
    )
    return exporters.csv_response(exporters.export_filename("stocktaking", period), header, rows)


@login_required
def export_stock(request):
    _require_reports(request)
    header, rows = exporters.stock_rows(get_stock_report())
    return exporters.csv_response(exporters.export_filename("stock"), header, rows)


@login_required
def export_low_stock(request):
    _require_reports(request)
    header, rows = exporters.low_stock_rows(get_low_stock_report())
    return exporters.csv_response(exporters.export_filename("low-stock"), header, rows)
