"""Слой 21/22 — экраны и CSV-экспорт отчётов. View только вызывает сервисы и
рендерит/отдаёт файл (read-only).

Денежные блоки скрываются в шаблоне по `can_view_purchase_cost` (`show_costs`);
доступ к разделу — по `can_view_reports`. Экспорт (Слой 22) использует ТЕ ЖЕ
сервисы и право, что UI; финансовые колонки пишутся только при purchase_cost.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.catalog.models import PartType

from . import exporters
from .payment_status import payment_statuses_for_rows
from .services import (
    CLIENTS_SORT_DATE,
    CLIENTS_SORT_DOCUMENTS,
    CLIENTS_SORTS,
    attach_customer_part_identity,
    attach_line_part_identity,
    attach_line_reversals,
    get_client_part_history,
    get_clients_sales_and_repairs,
    get_customer_part_operations,
    get_customer_repair_operations,
    get_dashboard_report,
    get_low_stock_report,
    get_repairs_by_customer,
    get_repairs_report,
    get_returns_report,
    get_sales_by_customer,
    get_sales_report,
    get_stock_report,
    get_stocktaking_report,
    get_writeoffs_report,
    order_clients_rows,
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


def _can_manage_payment_acknowledgements(request) -> bool:
    """Payment acknowledgement is a managerial finance action, never a viewer action."""
    return request.user.is_admin or request.user.is_manager


def _period_query(period) -> str:
    return urlencode(
        {
            "date_from": period.date_from.isoformat(),
            "date_to": period.date_to.isoformat(),
        }
    )


def _customer_selection(request) -> tuple[str, bool, int | None]:
    """Кого показывать: карточку по id или исторические документы по имени."""
    raw_id = (request.GET.get("customer_id") or "").strip()
    if raw_id:
        try:
            return "", False, int(raw_id)
        except ValueError as exc:
            raise Http404("Клиент не указан.") from exc
    missing = request.GET.get("missing") == "1"
    customer_name = (request.GET.get("customer") or "").strip()
    if not missing and not customer_name:
        raise Http404("Клиент не указан.")
    return customer_name, missing, None


def _customer_query(customer_name: str, missing: bool, customer_id=None) -> str:
    if customer_id:
        return urlencode({"customer_id": customer_id})
    return urlencode({"missing": "1"} if missing else {"customer": customer_name})


def _row_query(row) -> str:
    """Ссылка на детализацию строки отчёта с учётом её идентичности."""
    if row.get("linked"):
        return urlencode({"customer_id": row["customer_id"]})
    name = row.get("report_customer") or ""
    return urlencode({"missing": "1"} if not name else {"customer": name})


def _customer_title(customer_name: str, missing: bool, customer_id=None) -> str:
    if customer_id:
        from apps.customers.models import Customer

        customer = Customer.objects.filter(pk=customer_id).first()
        if customer is None:
            raise Http404("Карточка клиента не найдена.")
        return customer.name
    return customer_name or "Без клиента"


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
        # display_name и linked приходят из агрегата: карточка показывается
        # своим текущим именем, документы без карточки помечаются отдельно.
        row["customer_qs"] = _row_query(row)
    return render(
        request,
        "reports/sales_by_client.html",
        {
            "period": period,
            "period_qs": _period_query(period),
            "presets": _PRESETS,
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": True,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def sales_by_client_detail(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    customer_name, missing, customer_id = _customer_selection(request)
    # Плоская история: строка на проданную деталь, а не документ продажи.
    # Вопрос сотрудника звучит «что мы продавали этому клиенту», и номер
    # документа на него не отвечает.
    page_obj, is_paginated = _paginate(
        request,
        get_customer_part_operations(
            period,
            customer_name=customer_name,
            missing=missing,
            customer_id=customer_id,
        ),
    )
    page_obj.object_list = attach_line_part_identity(page_obj.object_list)
    # Сколько из каждой строки уже вернулось: обычным возвратом или отменой.
    # Одним запросом на страницу, иначе плоская история клиента дала бы запрос
    # на строку.
    attach_line_reversals(page_obj.object_list)
    return render(
        request,
        "reports/sales_by_client_detail.html",
        {
            # Отмена позиции возвращает товар на склад, поэтому её видит тот,
            # кому разрешён возврат: то же правило, что у отмены документа.
            "can_cancel_lines": (
                request.user.can_manage_sales and request.user.can_manage_returns
            ),
            "customer_name": _customer_title(customer_name, missing, customer_id),
            "customer_value": customer_name,
            "customer_qs": _customer_query(customer_name, missing, customer_id),
            "period": period,
            "period_qs": _period_query(period),
            "presets": _PRESETS,
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": True,
            "show_costs": request.user.can_view_purchase_cost,
            "missing_customer": missing,
        },
    )


def _clients_sort(request) -> tuple[str, str]:
    """Только разрешённый порядок из адреса, иначе прежнее умолчание.

    Незнакомое или испорченное значение молча возвращает отчёт к умолчанию:
    сортировка не тот повод, чтобы показывать оператору ошибку.
    """
    sort = request.GET.get("sort", CLIENTS_SORT_DOCUMENTS)
    direction = request.GET.get("direction", "desc")
    if sort not in CLIENTS_SORTS or direction not in {"asc", "desc"}:
        return CLIENTS_SORT_DOCUMENTS, "desc"
    return sort, direction


@login_required
def clients_overview(request):
    """Продажи и ремонты по клиентам в одной таблице.

    Итог с клиента складывает продажи и историческую стоимость деталей в
    ремонтах. Себестоимость остаётся отдельной ограниченной величиной.
    Сортировка меняет только порядок строк, но не сами суммы.
    """
    _require_reports(request)
    period = resolve_period(request.GET)
    sort, direction = _clients_sort(request)
    rows = order_clients_rows(
        get_clients_sales_and_repairs(period), sort=sort, direction=direction
    )
    page_obj, is_paginated = _paginate(request, rows)
    for row in page_obj.object_list:
        row["customer_qs"] = _row_query(row)
    by_date = sort == CLIENTS_SORT_DATE
    # Статус оплаты считается по СТРАНИЦЕ, а не по всему отчёту: порядок строк
    # уже выбран, и лишние клиенты сюда не попадают.
    payment_statuses = payment_statuses_for_rows(rows=page_obj.object_list, period=period)
    for row in page_obj.object_list:
        row["payment_status"] = payment_statuses.get(row.get("customer_id"))
    return render(
        request,
        "reports/clients_overview.html",
        {
            "period": period,
            "period_qs": _period_query(period),
            "presets": _PRESETS,
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": True,
            "show_costs": request.user.can_view_purchase_cost,
            "date_sort": {
                "active": by_date,
                "newest_first": by_date and direction != "asc",
                # Клик по заголовку переворачивает порядок; с умолчания
                # первый клик даёт «сначала новые» - у даты это ожидаемое.
                "next_direction": "asc" if by_date and direction != "asc" else "desc",
            },
            # Смена периода не должна сбрасывать выбранный порядок: форма
            # фильтра и быстрые пресеты несут его дальше сами.
            "active_sort": sort if by_date else "",
            "active_direction": direction if by_date else "",
            "sort_qs": urlencode({"sort": sort, "direction": direction}) if by_date else "",
            "can_manage_payment_acknowledgements": _can_manage_payment_acknowledgements(request),
        },
    )


@login_required
@require_POST
def client_period_payment_status(request):
    """Create or revoke a payment acknowledgement after server-side recomputation."""
    _require_reports(request)
    if not _can_manage_payment_acknowledgements(request):
        raise PermissionDenied
    period = resolve_period(request.POST)
    try:
        customer_id = int(request.POST.get("customer_id") or "")
    except ValueError as exc:
        raise Http404("Клиент не указан.") from exc

    from apps.customers.models import Customer
    from apps.customers.services import (
        PaymentAcknowledgementError,
        acknowledge_customer_period_payment,
        revoke_customer_period_payment,
    )
    get_object_or_404(Customer, pk=customer_id)

    if request.POST.get("paid") == "1":
        try:
            acknowledge_customer_period_payment(
                customer_id=customer_id, period=period, by=request.user
            )
        except PaymentAcknowledgementError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Оплата клиента за выбранный период подтверждена.")
    else:
        revoke_customer_period_payment(customer_id=customer_id, period=period, by=request.user)
        messages.success(request, "Подтверждение оплаты за выбранный период снято.")
    return redirect(f"{reverse('reports_clients_overview')}?{_period_query(period)}")


@login_required
def client_timeline(request):
    """Единая лента документов клиента: продажи и ремонты по времени."""
    _require_reports(request)
    period = resolve_period(request.GET)
    customer_name, missing, customer_id = _customer_selection(request)
    history = get_client_part_history(
        period, customer_name=customer_name, missing=missing, customer_id=customer_id
    )
    page_obj, is_paginated = _paginate(request, history)
    sales_total = sum((row["amount"] or 0) for row in history if row["kind"] == "sale")
    repair_rows = [row for row in history if row["kind"] == "repair"]
    repair_unknown = any(row["amount"] is None for row in repair_rows)
    repair_total = sum((row["amount"] or 0) for row in repair_rows)
    return render(
        request,
        "reports/client_timeline.html",
        {
            "customer_name": _customer_title(customer_name, missing, customer_id),
            "customer_qs": _customer_query(customer_name, missing, customer_id),
            "period": period,
            "period_qs": _period_query(period),
            "presets": _PRESETS,
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": True,
            "show_costs": request.user.can_view_purchase_cost,
            "client_summary": {
                "sales": sales_total,
                "repairs": repair_total,
                "total": sales_total + repair_total,
                "unknown": repair_unknown,
            },
        },
    )


@login_required
def repairs_by_client(request):
    """Ремонты по клиентам.

    Денег клиента здесь нет: ремонтный заказ фиксирует выдачу деталей и их
    себестоимость, цены работ система не хранит. Поэтому в отчёте нет колонки
    выручки, а «Себестоимость выданного» показывается только тем, кому открыты
    закупочные цены.
    """
    _require_reports(request)
    period = resolve_period(request.GET)
    page_obj, is_paginated = _paginate(request, get_repairs_by_customer(period))
    for row in page_obj.object_list:
        # display_name и linked приходят из агрегата: карточка показывается
        # своим текущим именем, документы без карточки помечаются отдельно.
        row["customer_qs"] = _row_query(row)
    return render(
        request,
        "reports/repairs_by_client.html",
        {
            "period": period,
            "period_qs": _period_query(period),
            "presets": _PRESETS,
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": True,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def repairs_by_client_detail(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    customer_name, missing, customer_id = _customer_selection(request)
    # Плоская история выдач: строка на деталь, а не ремонтный заказ.
    page_obj, is_paginated = _paginate(
        request,
        get_customer_repair_operations(
            period, customer_name=customer_name, missing=missing, customer_id=customer_id
        ),
    )
    page_obj.object_list = attach_line_part_identity(page_obj.object_list)
    from apps.repairs.services import repair_customer_line_amounts, repair_returned_quantities

    amounts = repair_customer_line_amounts(page_obj.object_list)
    returned = repair_returned_quantities(page_obj.object_list)
    for line in page_obj.object_list:
        line.net_quantity = max(line.quantity - (returned.get(line.pk) or 0), 0)
        line.customer_amount_rub = amounts[line.pk]
        line.net_cost_rub = line.unit_cost_rub * line.net_quantity
    return render(
        request,
        "reports/repairs_by_client_detail.html",
        {
            "customer_name": _customer_title(customer_name, missing, customer_id),
            "customer_value": customer_name,
            "customer_qs": _customer_query(customer_name, missing, customer_id),
            "period": period,
            "period_qs": _period_query(period),
            "presets": _PRESETS,
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": True,
            "show_costs": request.user.can_view_purchase_cost,
            "missing_customer": missing,
        },
    )


@login_required
def repairs_by_client_operations(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    customer_name, missing, customer_id = _customer_selection(request)
    try:
        part_id = int(request.GET.get("part", ""))
    except (TypeError, ValueError) as exc:
        raise Http404("Деталь не указана.") from exc
    part = get_object_or_404(PartType, pk=part_id)
    page_obj, is_paginated = _paginate(
        request,
        get_customer_repair_operations(
            period,
            customer_name=customer_name,
            missing=missing,
            customer_id=customer_id,
            part_type_id=part.pk,
        ),
    )
    identity = attach_customer_part_identity([{"part_type_id": part.pk}])[0]
    return render(
        request,
        "reports/repairs_by_client_operations.html",
        {
            "customer_name": _customer_title(customer_name, missing, customer_id),
            "customer_qs": _customer_query(customer_name, missing, customer_id),
            "part": part,
            "exact_number": identity["exact_number"],
            "period": period,
            "period_qs": _period_query(period),
            "page_obj": page_obj,
            "is_paginated": is_paginated,
            "show_money": request.user.can_view_purchase_cost,
        },
    )


@login_required
def sales_by_client_operations(request):
    _require_reports(request)
    period = resolve_period(request.GET)
    customer_name, missing, customer_id = _customer_selection(request)
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
            customer_id=customer_id,
            part_type_id=part.pk,
        ),
    )
    identity = attach_customer_part_identity([{"part_type_id": part.pk}])[0]
    return render(
        request,
        "reports/sales_by_client_operations.html",
        {
            "customer_name": _customer_title(customer_name, missing, customer_id),
            "customer_qs": _customer_query(customer_name, missing, customer_id),
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
