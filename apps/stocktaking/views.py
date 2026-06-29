"""Слой 20 — экраны инвентаризации. View — оркестратор.

Любая мутация остатка/документа идёт через `apps.stocktaking.services`; вьюхи сами
в `StockMovement`/`StockBalance`/`StockLot.quantity` не пишут. Hidden/query-параметры
недоверенные: документ/лот/строка всегда перечитываются из БД,
права/статус/доступность/количество/резерв проверяет сервис.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import AddCountLotForm, CountQuantityForm, InventoryCountForm
from .models import InventoryCountDocument, InventoryCountLine
from .services import (
    StocktakingError,
    add_stock_lot_count_line,
    cancel_inventory_count,
    complete_inventory_count,
    create_inventory_count,
    remove_count_line,
    update_counted_quantity,
)


def _require_stocktaking(request) -> None:
    if not request.user.can_manage_stocktaking:
        raise PermissionDenied


@login_required
def inventory_count_list(request):
    status = request.GET.get("status", "")
    qs = (
        InventoryCountDocument.objects.select_related("created_by", "scope_location")
        .order_by("-created_at")
    )
    if status:
        qs = qs.filter(status=status)
    return render(
        request,
        "stocktaking/inventory_count_list.html",
        {
            "documents": qs[:100],
            "status": status,
            "statuses": InventoryCountDocument.Status.choices,
            "can_manage": request.user.can_manage_stocktaking,
        },
    )


@login_required
def inventory_count_detail(request, pk):
    doc = get_object_or_404(
        InventoryCountDocument.objects.select_related("created_by", "scope_location"), pk=pk
    )
    lines = doc.lines.select_related(
        "part_type", "stock_lot", "stock_lot__location", "location", "adjustment"
    )
    is_draft = doc.status == InventoryCountDocument.Status.DRAFT
    return render(
        request,
        "stocktaking/inventory_count_detail.html",
        {
            "doc": doc,
            "lines": lines,
            "can_manage": request.user.can_manage_stocktaking,
            "is_draft": is_draft,
            "show_costs": request.user.can_view_purchase_cost,
            "add_lot_form": AddCountLotForm(location=doc.scope_location),
            "count_form": CountQuantityForm(),
        },
    )


@login_required
def inventory_count_create(request):
    _require_stocktaking(request)
    if request.method == "POST":
        form = InventoryCountForm(request.POST)
        if form.is_valid():
            doc = create_inventory_count(
                scope_location=form.cleaned_data["scope_location"],
                comment=form.cleaned_data["comment"],
                by=request.user,
            )
            messages.success(request, f"Инвентаризация {doc.number} создана.")
            return redirect("inventory_count_detail", pk=doc.pk)
    else:
        initial = {}
        loc_id = request.GET.get("location")
        if loc_id and loc_id.isdigit():
            initial["scope_location"] = loc_id  # подсказка из карточки лота
        form = InventoryCountForm(initial=initial)
    return render(request, "stocktaking/inventory_count_form.html", {"form": form})


@login_required
@require_POST
def inventory_count_add_lot(request, pk):
    _require_stocktaking(request)
    doc = get_object_or_404(InventoryCountDocument, pk=pk)
    form = AddCountLotForm(request.POST, location=doc.scope_location)
    if not form.is_valid():
        messages.error(request, "Проверьте выбранный лот.")
        return redirect("inventory_count_detail", pk=pk)
    try:
        add_stock_lot_count_line(doc, form.cleaned_data["lot"], by=request.user)
    except StocktakingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Лот добавлен в документ.")
    return redirect("inventory_count_detail", pk=pk)


@login_required
@require_POST
def inventory_count_set_count(request, pk):
    _require_stocktaking(request)
    line = get_object_or_404(InventoryCountLine, pk=pk)
    doc_pk = line.count_document_id
    form = CountQuantityForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте фактическое количество.")
        return redirect("inventory_count_detail", pk=doc_pk)
    try:
        update_counted_quantity(line, form.cleaned_data["counted"], by=request.user)
    except StocktakingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Факт сохранён.")
    return redirect("inventory_count_detail", pk=doc_pk)


@login_required
@require_POST
def inventory_count_remove_line(request, pk):
    _require_stocktaking(request)
    line = get_object_or_404(InventoryCountLine, pk=pk)
    doc_pk = line.count_document_id
    try:
        remove_count_line(line, by=request.user)
    except StocktakingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Строка снята.")
    return redirect("inventory_count_detail", pk=doc_pk)


@login_required
@require_POST
def inventory_count_complete(request, pk):
    _require_stocktaking(request)
    doc = get_object_or_404(InventoryCountDocument, pk=pk)
    try:
        complete_inventory_count(doc, by=request.user)
    except StocktakingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Инвентаризация {doc.number} проведена.")
    return redirect("inventory_count_detail", pk=pk)


@login_required
@require_POST
def inventory_count_cancel(request, pk):
    _require_stocktaking(request)
    doc = get_object_or_404(InventoryCountDocument, pk=pk)
    try:
        cancel_inventory_count(doc, by=request.user)
    except StocktakingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Инвентаризация {doc.number} отменена.")
    return redirect("inventory_count_detail", pk=pk)
