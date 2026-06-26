"""Слой 19 — экраны документированного списания. View — оркестратор.

Любая мутация остатка/документа идёт через `apps.writeoffs.services`; вьюхи сами в
`StockMovement`/`StockBalance`/`PartItem.status`/`StockLot.quantity` не пишут.
Hidden/query-параметры недоверенные: объект всегда перечитывается из БД,
права/статус/резерв/доступность/количество проверяет сервис.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.inventory.models import PartItem

from .forms import AddWriteOffItemForm, AddWriteOffLotForm, WriteOffForm
from .models import WriteOffDocument, WriteOffLine
from .services import (
    WriteOffError,
    add_part_item_to_write_off,
    add_stock_lot_to_write_off,
    cancel_write_off,
    complete_write_off,
    create_write_off,
    remove_write_off_line,
)


def _require_write_offs(request) -> None:
    if not request.user.can_manage_write_offs:
        raise PermissionDenied


def _resolve_item(code: str):
    """Найти PartItem по внутр. номеру/штрихкоду/серийнику (недоверенный ввод)."""
    code = (code or "").strip()
    if not code:
        return None
    return (
        PartItem.objects.filter(
            Q(internal_number__iexact=code)
            | Q(internal_barcode__iexact=code)
            | Q(serial_number__iexact=code)
        )
        .select_related("part_type", "current_location", "batch_line")
        .first()
    )


@login_required
def write_off_list(request):
    status = request.GET.get("status", "")
    qs = WriteOffDocument.objects.select_related("created_by").order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    return render(
        request,
        "writeoffs/write_off_list.html",
        {
            "documents": qs[:100],
            "status": status,
            "statuses": WriteOffDocument.Status.choices,
            "can_manage": request.user.can_manage_write_offs,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def write_off_detail(request, pk):
    doc = get_object_or_404(WriteOffDocument.objects.select_related("created_by"), pk=pk)
    lines = doc.lines.select_related(
        "part_type",
        "part_item",
        "part_item__current_location",
        "stock_lot",
        "stock_lot__location",
    )
    is_draft = doc.status == WriteOffDocument.Status.DRAFT
    return render(
        request,
        "writeoffs/write_off_detail.html",
        {
            "doc": doc,
            "lines": lines,
            "can_manage": request.user.can_manage_write_offs,
            "is_draft": is_draft,
            "show_costs": request.user.can_view_purchase_cost,
            "add_item_form": AddWriteOffItemForm(),
            "add_lot_form": AddWriteOffLotForm(),
        },
    )


@login_required
def write_off_create(request):
    _require_write_offs(request)
    if request.method == "POST":
        form = WriteOffForm(request.POST)
        if form.is_valid():
            try:
                doc = create_write_off(
                    reason=form.cleaned_data["reason"],
                    comment=form.cleaned_data["comment"],
                    by=request.user,
                )
            except WriteOffError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Документ списания {doc.number} создан.")
                return redirect("write_off_detail", pk=doc.pk)
    else:
        form = WriteOffForm()
    return render(request, "writeoffs/write_off_form.html", {"form": form})


@login_required
@require_POST
def write_off_add_item(request, pk):
    _require_write_offs(request)
    doc = get_object_or_404(WriteOffDocument, pk=pk)
    form = AddWriteOffItemForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте код экземпляра.")
        return redirect("write_off_detail", pk=pk)
    item = _resolve_item(form.cleaned_data["code"])
    if item is None:
        messages.error(request, "Экземпляр по коду не найден.")
    else:
        try:
            add_part_item_to_write_off(doc, item, by=request.user)
        except WriteOffError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Экземпляр {item.internal_number} добавлен.")
    return redirect("write_off_detail", pk=pk)


@login_required
@require_POST
def write_off_add_lot(request, pk):
    _require_write_offs(request)
    doc = get_object_or_404(WriteOffDocument, pk=pk)
    form = AddWriteOffLotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте лот и количество.")
        return redirect("write_off_detail", pk=pk)
    try:
        add_stock_lot_to_write_off(
            doc, form.cleaned_data["lot"], form.cleaned_data["quantity"], by=request.user
        )
    except WriteOffError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Количество из лота добавлено в документ.")
    return redirect("write_off_detail", pk=pk)


@login_required
@require_POST
def write_off_remove_line(request, pk):
    _require_write_offs(request)
    line = get_object_or_404(WriteOffLine, pk=pk)
    doc_pk = line.write_off_id
    try:
        remove_write_off_line(line, by=request.user)
    except WriteOffError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Позиция снята с документа.")
    return redirect("write_off_detail", pk=doc_pk)


@login_required
@require_POST
def write_off_complete(request, pk):
    _require_write_offs(request)
    doc = get_object_or_404(WriteOffDocument, pk=pk)
    try:
        complete_write_off(doc, by=request.user)
    except WriteOffError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Документ {doc.number} проведён — детали списаны.")
    return redirect("write_off_detail", pk=pk)


@login_required
@require_POST
def write_off_cancel(request, pk):
    _require_write_offs(request)
    doc = get_object_or_404(WriteOffDocument, pk=pk)
    try:
        cancel_write_off(doc, by=request.user)
    except WriteOffError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Документ {doc.number} отменён.")
    return redirect("write_off_detail", pk=pk)
