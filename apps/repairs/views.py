"""Слой 17 — экраны выдачи деталей в ремонт. View — оркестратор.

Любая мутация остатка/заказа идёт через `apps.repairs.services`; вьюхи сами в
`StockMovement`/`StockBalance`/`PartItem.status`/`StockLot.quantity` не пишут.
Hidden/query-параметры недоверенные: объект всегда перечитывается из БД,
права/статус/доступность/количество проверяет сервис.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.inventory.models import PartItem

from .forms import AddRepairItemForm, AddRepairLotForm, RepairOrderForm
from .models import RepairIssueLine, RepairOrder
from .services import (
    RepairError,
    add_part_item_to_repair_order,
    add_stock_lot_to_repair_order,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
    remove_repair_line,
)


def _require_repairs(request) -> None:
    if not request.user.can_manage_repairs:
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
def repair_order_list(request):
    status = request.GET.get("status", "")
    qs = RepairOrder.objects.select_related("created_by", "vehicle_type").order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    return render(
        request,
        "repairs/repair_order_list.html",
        {
            "orders": qs[:100],
            "status": status,
            "statuses": RepairOrder.Status.choices,
            "can_manage": request.user.can_manage_repairs,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def repair_order_detail(request, pk):
    order = get_object_or_404(
        RepairOrder.objects.select_related("created_by", "vehicle_type"), pk=pk
    )
    lines = order.lines.select_related(
        "part_type",
        "part_item",
        "part_item__current_location",
        "stock_lot",
        "stock_lot__location",
    )
    is_draft = order.status == RepairOrder.Status.DRAFT
    return render(
        request,
        "repairs/repair_order_detail.html",
        {
            "order": order,
            "lines": lines,
            "can_manage": request.user.can_manage_repairs,
            "can_return": request.user.can_manage_returns,
            "is_draft": is_draft,
            "show_costs": request.user.can_view_purchase_cost,
            "add_item_form": AddRepairItemForm(),
            "add_lot_form": AddRepairLotForm(),
        },
    )


@login_required
def repair_order_create(request):
    _require_repairs(request)
    if request.method == "POST":
        form = RepairOrderForm(request.POST)
        if form.is_valid():
            try:
                order = create_repair_order(
                    customer_name=form.cleaned_data["customer_name"],
                    customer_phone=form.cleaned_data["customer_phone"],
                    vehicle_type=form.cleaned_data["vehicle_type"],
                    vehicle_make=form.cleaned_data["vehicle_make"],
                    vehicle_model=form.cleaned_data["vehicle_model"],
                    vehicle_identifier=form.cleaned_data["vehicle_identifier"],
                    problem_description=form.cleaned_data["problem_description"],
                    comment=form.cleaned_data["comment"],
                    by=request.user,
                )
            except RepairError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Ремонтный заказ {order.number} создан.")
                return redirect("repair_order_detail", pk=order.pk)
    else:
        form = RepairOrderForm()
    return render(request, "repairs/repair_order_form.html", {"form": form})


@login_required
@require_POST
def repair_order_add_item(request, pk):
    _require_repairs(request)
    order = get_object_or_404(RepairOrder, pk=pk)
    form = AddRepairItemForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте код экземпляра.")
        return redirect("repair_order_detail", pk=pk)
    item = _resolve_item(form.cleaned_data["code"])
    if item is None:
        messages.error(request, "Экземпляр по коду не найден.")
    else:
        try:
            add_part_item_to_repair_order(order, item, by=request.user)
        except RepairError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Экземпляр {item.internal_number} добавлен.")
    return redirect("repair_order_detail", pk=pk)


@login_required
@require_POST
def repair_order_add_lot(request, pk):
    _require_repairs(request)
    order = get_object_or_404(RepairOrder, pk=pk)
    form = AddRepairLotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте лот и количество.")
        return redirect("repair_order_detail", pk=pk)
    try:
        add_stock_lot_to_repair_order(
            order, form.cleaned_data["lot"], form.cleaned_data["quantity"], by=request.user
        )
    except RepairError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Количество из лота добавлено в заказ.")
    return redirect("repair_order_detail", pk=pk)


@login_required
@require_POST
def repair_order_remove_line(request, pk):
    _require_repairs(request)
    line = get_object_or_404(RepairIssueLine, pk=pk)
    order_pk = line.repair_order_id
    try:
        remove_repair_line(line, by=request.user)
    except RepairError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Позиция снята с заказа.")
    return redirect("repair_order_detail", pk=order_pk)


@login_required
@require_POST
def repair_order_complete(request, pk):
    _require_repairs(request)
    order = get_object_or_404(RepairOrder, pk=pk)
    try:
        complete_repair_order(order, by=request.user)
    except RepairError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Заказ {order.number} проведён — детали выданы в ремонт.")
    return redirect("repair_order_detail", pk=pk)


@login_required
@require_POST
def repair_order_cancel(request, pk):
    _require_repairs(request)
    order = get_object_or_404(RepairOrder, pk=pk)
    try:
        cancel_repair_order(order, by=request.user)
    except RepairError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Заказ {order.number} отменён.")
    return redirect("repair_order_detail", pk=pk)
