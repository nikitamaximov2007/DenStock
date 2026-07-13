"""Слой 18 — экраны возвратов на склад. View — оркестратор.

Любая мутация остатка/возврата идёт через `apps.returns.services`; вьюхи сами в
`StockMovement`/`StockBalance`/`PartItem.status`/`StockLot.quantity` не пишут.
Hidden/query-параметры недоверенные: источник/строка/ячейка/количество всегда
перечитываются из БД, права/статус/доступность к возврату проверяет сервис.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.inventory.presentation import (
    attach_document_composition,
    attach_part_identity,
    lines_with_identity_prefetch,
    with_part_identity,
)
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale, SaleLine
from apps.warehouse.models import StorageLocation

from .forms import AddReturnLineForm, ReturnForm, ReturnLineRestockStatusForm
from .models import StockReturn, StockReturnLine
from .services import (
    ReturnError,
    add_repair_line_return,
    add_sale_line_return,
    complete_return,
    create_return,
    get_source,
    get_source_lines,
    remove_return_line,
    resolve_source_line,
    returnable_quantities,
    source_location_for_repair_line,
    update_return_line_restock_status,
)


def _require_returns(request) -> None:
    if not request.user.can_manage_returns:
        raise PermissionDenied


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_source(request):
    """Объект-источник из ?source=sale|repair&id=… (недоверенный ввод)."""
    src = request.POST.get("source") or request.GET.get("source")
    sid = _int(request.POST.get("id") or request.GET.get("id"))
    if sid is None:
        return None, ""
    if src == "sale":
        return Sale.objects.filter(pk=sid).first(), "sale"
    if src == "repair":
        return RepairOrder.objects.filter(pk=sid).first(), "repair"
    return None, ""


@login_required
def return_list(request):
    status = request.GET.get("status", "")
    qs = (
        StockReturn.objects.select_related("created_by")
        .prefetch_related(lines_with_identity_prefetch(StockReturnLine))
        .order_by("-created_at")
    )
    if status:
        qs = qs.filter(status=status)
    returns = list(qs[:100])
    repair_ids = {
        ret.source_id
        for ret in returns
        if ret.source_type == StockReturn.SourceType.REPAIR_ORDER
    }
    sale_ids = {
        ret.source_id for ret in returns if ret.source_type == StockReturn.SourceType.SALE
    }
    repair_numbers = dict(
        RepairOrder.objects.filter(pk__in=repair_ids).values_list("pk", "number")
    )
    sale_numbers = dict(Sale.objects.filter(pk__in=sale_ids).values_list("pk", "number"))
    cost_by_return = dict(
        StockReturnLine.objects.filter(stock_return__in=returns)
        .values("stock_return_id")
        .annotate(total=Sum("total_cost_rub"))
        .values_list("stock_return_id", "total")
    )
    for ret in returns:
        if ret.source_type == StockReturn.SourceType.REPAIR_ORDER:
            ret.source_label = f"Ремонт {repair_numbers.get(ret.source_id, 'не найден')}"
        else:
            ret.source_label = f"Продажа {sale_numbers.get(ret.source_id, 'не найдена')}"
        ret.display_cost_total = ret.cost_total if ret.status != StockReturn.Status.DRAFT else (
            cost_by_return.get(ret.pk) or Decimal("0")
        )
    attach_document_composition(returns)  # состав: первая позиция + «ещё N»
    return render(
        request,
        "returns/return_list.html",
        {
            "returns": returns,
            "status": status,
            "statuses": StockReturn.Status.choices,
            "can_manage": request.user.can_manage_returns,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def return_detail(request, pk):
    ret = get_object_or_404(
        StockReturn.objects.select_related("created_by", "completed_by"), pk=pk
    )
    source = get_source(ret)
    is_draft = ret.status == StockReturn.Status.DRAFT
    source_rows = []
    if source is not None and is_draft:
        source_lines = list(with_part_identity(get_source_lines(source)))
        attach_part_identity(source_lines)  # exact-артикул и в таблице «к возврату»
        returnable_by_source = returnable_quantities(source_lines, draft=ret)
        for sl in source_lines:
            avail = returnable_by_source[sl.pk]
            if avail > 0:
                source_location = (
                    source_location_for_repair_line(sl)
                    if ret.source_type == StockReturn.SourceType.REPAIR_ORDER
                    else None
                )
                source_rows.append(
                    {
                        "line": sl,
                        "returnable": avail,
                        "is_item": sl.part_item_id is not None,
                        "source_location": source_location,
                    }
                )
    lines = list(
        with_part_identity(
            ret.lines.select_related(
                "part_type", "part_item", "stock_lot", "to_location", "returned_lot"
            )
        )
    )
    attach_part_identity(lines)  # exact-артикул отдельной колонкой
    ret.display_cost_total = (
        ret.cost_total
        if not is_draft
        else sum((line.total_cost_rub for line in lines), Decimal("0"))
    )
    return render(
        request,
        "returns/return_detail.html",
        {
            "ret": ret,
            "source": source,
            "source_rows": source_rows,
            "lines": lines,
            "is_draft": is_draft,
            "can_manage": request.user.can_manage_returns,
            "show_costs": request.user.can_view_purchase_cost,
            "restock_choices": StockReturnLine.RestockStatus.choices,
            "locations": (
                StorageLocation.objects.filter(is_active=True, storage_allowed=True)
                .order_by("code")
                if is_draft else StorageLocation.objects.none()
            ),
        },
    )


@login_required
def return_create(request):
    _require_returns(request)
    source, source_key = _resolve_source(request)
    if source is None:
        messages.error(
            request, "Источник возврата не найден (нужна проведённая продажа или ремонт)."
        )
        return redirect("return_list")
    if request.method == "POST":
        form = ReturnForm(request.POST)
        if form.is_valid():
            try:
                ret = create_return(
                    source=source,
                    reason=form.cleaned_data["reason"],
                    comment=form.cleaned_data["comment"],
                    by=request.user,
                )
            except ReturnError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Возврат {ret.number} создан — добавьте позиции.")
                return redirect("return_detail", pk=ret.pk)
    else:
        form = ReturnForm()
    return render(
        request,
        "returns/return_form.html",
        {"form": form, "source": source, "source_key": source_key},
    )


@login_required
@require_POST
def return_add_line(request, pk):
    _require_returns(request)
    ret = get_object_or_404(StockReturn, pk=pk)
    form = AddReturnLineForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте ячейку, состояние и количество.")
        return redirect("return_detail", pk=pk)
    source_line = resolve_source_line(ret, form.cleaned_data["source_line_id"])
    if source_line is None:
        messages.error(request, "Строка-источник не найдена.")
        return redirect("return_detail", pk=pk)
    quantity = form.cleaned_data.get("quantity") or Decimal("0")
    to_location = form.cleaned_data["to_location"]
    restock_status = form.cleaned_data["restock_status"]
    try:
        if isinstance(source_line, SaleLine):
            add_sale_line_return(
                ret, source_line, quantity,
                to_location=to_location, restock_status=restock_status, by=request.user,
            )
        else:
            add_repair_line_return(
                ret, source_line, quantity,
                to_location=to_location, restock_status=restock_status, by=request.user,
            )
    except ReturnError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Позиция добавлена в возврат.")
    return redirect("return_detail", pk=pk)


@login_required
@require_POST
def return_remove_line(request, pk):
    _require_returns(request)
    line = get_object_or_404(StockReturnLine, pk=pk)
    return_pk = line.stock_return_id
    try:
        remove_return_line(line, by=request.user)
    except ReturnError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Позиция снята с возврата.")
    return redirect("return_detail", pk=return_pk)


@login_required
@require_POST
def return_update_line_status(request, pk):
    _require_returns(request)
    line = get_object_or_404(StockReturnLine, pk=pk)
    return_pk = line.stock_return_id
    form = ReturnLineRestockStatusForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Выберите допустимое состояние возврата.")
        return redirect("return_detail", pk=return_pk)
    try:
        update_return_line_restock_status(
            line, restock_status=form.cleaned_data["restock_status"], by=request.user
        )
    except ReturnError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Состояние позиции возврата обновлено.")
    return redirect("return_detail", pk=return_pk)


@login_required
@require_POST
def return_complete(request, pk):
    _require_returns(request)
    ret = get_object_or_404(StockReturn, pk=pk)
    try:
        complete_return(ret, by=request.user)
    except ReturnError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Возврат {ret.number} проведён — остаток восстановлен.")
    return redirect("return_detail", pk=pk)
