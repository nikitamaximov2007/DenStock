"""Слой 21/22 — экраны и CSV-экспорт отчётов. View только вызывает сервисы и
рендерит/отдаёт файл (read-only).

Денежные блоки скрываются в шаблоне по `can_view_purchase_cost` (`show_costs`);
доступ к разделу — по `can_view_reports`. Экспорт (Слой 22) использует ТЕ ЖЕ
сервисы и право, что UI; финансовые колонки пишутся только при purchase_cost.
"""
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from apps.catalog.models import PartType

from . import exporters
from .services import (
    attach_customer_part_identity,
    get_customer_part_operations,
    get_customer_part_sales,
    get_customer_phones,
    get_dashboard_report,
    get_low_stock_report,
    get_repairs_report,
    get_returns_report,
    get_sales_by_customer,
    get_sales_report,
    get_stock_report,
    get_stocktaking_report,
    get_writeoffs_report,
    resolve_period,
)
from .statistics import STATS_PRESETS, get_statistics, resolve_stats_period

_PRESETS = [("today", "Сегодня"), ("7", "7 дней"), ("30", "30 дней"), ("month", "Месяц")]
_CLIENT_REPORT_PAGE_SIZE = 50


def _require_reports(request) -> None:
    if not request.user.can_view_reports:
        raise PermissionDenied


def _require_finance(request) -> None:
    if not request.user.can_view_finance:
        raise PermissionDenied


def _period_query(period) -> str:
    return urlencode(
        {
            "date_from": period.date_from.isoformat(),
            "date_to": period.date_to.isoformat(),
        }
    )


def _customer_selection(request) -> tuple[str, bool]:
    missing = request.GET.get("missing") == "1"
    customer_name = (request.GET.get("customer") or "").strip()
    if not missing and not customer_name:
        raise Http404("Клиент не указан.")
    return customer_name, missing


def _customer_query(customer_name: str, missing: bool) -> str:
    return urlencode({"missing": "1"} if missing else {"customer": customer_name})


def _paginate(request, rows):
    page_obj = Paginator(rows, _CLIENT_REPORT_PAGE_SIZE).get_page(request.GET.get("page"))
    return page_obj, page_obj.paginator.num_pages > 1


# --- Layer 27: «Статистика» — срез состояния склада (read-only) ----------------


@login_required
def statistics_dashboard(request):
    _require_finance(request)
    period = resolve_stats_period(request.GET)
    return render(
        request,
        "reports/statistics.html",
        {
            "stats": get_statistics(period),
            "period": period,
            "presets": STATS_PRESETS,
        },
    )


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


@login_required
def sales_by_client(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    page_obj, is_paginated = _paginate(request, get_sales_by_customer(period))
    for row in page_obj.object_list:
        customer_name = row["report_customer"]
        row["display_name"] = customer_name or "Без клиента"
        row["customer_qs"] = _customer_query(customer_name, not customer_name)
    return render(
        request,
        "reports/sales_by_client.html",
        {
            "period": period,
            "period_qs": _period_query(period),
            "presets": _PRESETS,
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": request.user.can_view_purchase_cost,
        },
    )


@login_required
def sales_by_client_detail(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    customer_name, missing = _customer_selection(request)
    page_obj, is_paginated = _paginate(
        request,
        get_customer_part_sales(
            period,
            customer_name=customer_name,
            missing=missing,
        ),
    )
    page_obj.object_list = attach_customer_part_identity(page_obj.object_list)
    return render(
        request,
        "reports/sales_by_client_detail.html",
        {
            "customer_name": customer_name or "Без клиента",
            "customer_value": customer_name,
            "customer_phones": get_customer_phones(
                period, customer_name=customer_name, missing=missing
            ),
            "customer_qs": _customer_query(customer_name, missing),
            "period": period,
            "period_qs": _period_query(period),
            "presets": _PRESETS,
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": request.user.can_view_purchase_cost,
            "missing_customer": missing,
        },
    )


@login_required
def sales_by_client_operations(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    customer_name, missing = _customer_selection(request)
    try:
        part_id = int(request.GET.get("part", ""))
    except (TypeError, ValueError) as exc:
        raise Http404("Деталь не указана.") from exc
    part = get_object_or_404(PartType, pk=part_id)
    page_obj, is_paginated = _paginate(
        request,
        get_customer_part_operations(
            period,
            customer_name=customer_name,
            missing=missing,
            part_type_id=part.pk,
        ),
    )
    identity = attach_customer_part_identity([{"part_type_id": part.pk}])[0]
    return render(
        request,
        "reports/sales_by_client_operations.html",
        {
            "customer_name": customer_name or "Без клиента",
            "customer_qs": _customer_query(customer_name, missing),
            "part": part,
            "exact_number": identity["exact_number"],
            "period": period,
            "period_qs": _period_query(period),
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": request.user.can_view_purchase_cost,
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
