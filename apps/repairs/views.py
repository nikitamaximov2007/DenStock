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
from apps.inventory.presentation import (
    attach_document_composition,
    attach_part_identity,
    lines_with_identity_prefetch,
    with_part_identity,
)

from .forms import AddRepairItemForm, AddRepairLotForm, RepairCancellationForm, RepairOrderForm
from .models import RepairIssueLine, RepairOrder
from .services import (
    RepairError,
    add_part_item_to_repair_order,
    add_stock_lot_to_repair_order,
    calculate_repair_customer_amount,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
    remove_repair_line,
    repair_customer_line_amounts,
    repair_customer_line_prices,
    repair_returned_quantities,
    set_repair_line_customer_price,
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
    qs = (
        RepairOrder.objects.select_related("created_by", "vehicle_type")
        .prefetch_related(lines_with_identity_prefetch(RepairIssueLine))
        .order_by("-created_at")
    )
    if status:
        qs = qs.filter(status=status)
    orders = list(qs[:100])
    attach_document_composition(orders)  # состав: первая позиция + «ещё N»
    return render(
        request,
        "repairs/repair_order_list.html",
        {
            "orders": orders,
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
    lines = list(
        with_part_identity(
            order.lines.select_related(
                "part_type",
                "part_item",
                "part_item__current_location",
                "stock_lot",
                "stock_lot__location",
            )
        )
    )
    attach_part_identity(lines)  # exact-артикул отдельной колонкой
    for line in lines:
        line.customer_total_rub = None
    if order.status == RepairOrder.Status.COMPLETED:
        amounts = repair_customer_line_amounts(lines)
        prices = repair_customer_line_prices(lines)
        returned = repair_returned_quantities(lines)
        for line in lines:
            line.customer_total_rub = amounts[line.pk]
            line.customer_display_unit_price_rub = prices[line.pk].unit_price_rub
            line.customer_price_source = prices[line.pk].source
            line.net_quantity = max(line.quantity - (returned.get(line.pk) or 0), 0)
    else:
        for line in lines:
            line.customer_total_rub = (
                None
                if line.customer_unit_price_rub is None
                else line.customer_unit_price_rub * line.quantity
            )
            line.customer_display_unit_price_rub = line.customer_unit_price_rub
            line.customer_price_source = "historical"
            line.net_quantity = line.quantity
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
            "customer_amount": calculate_repair_customer_amount(order)
            if order.status == RepairOrder.Status.COMPLETED
            else None,
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
                    customer=form.cleaned_data.get("customer"),
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
            add_part_item_to_repair_order(
                order,
                item,
                customer_unit_price_rub=form.cleaned_data["customer_unit_price_rub"],
                by=request.user,
            )
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
            order,
            form.cleaned_data["lot"],
            form.cleaned_data["quantity"],
            customer_unit_price_rub=form.cleaned_data["customer_unit_price_rub"],
            by=request.user,
        )
    except RepairError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Количество из лота добавлено в заказ.")
    return redirect("repair_order_detail", pk=pk)


@login_required
@require_POST
def repair_order_set_line_price(request, pk):
    _require_repairs(request)
    line = get_object_or_404(RepairIssueLine, pk=pk)
    try:
        set_repair_line_customer_price(
            line, request.POST.get("customer_unit_price_rub"), by=request.user
        )
    except RepairError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Цена детали для клиента сохранена.")
    return redirect("repair_order_detail", pk=line.repair_order_id)


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
    # У проведённого заказа отмена возвращает выданные детали на склад, а это
    # складское действие: продавцу возврат не выдан именно затем, чтобы выдачу
    # нельзя было отменить в обход. Черновик склада не касается.
    if order.status == RepairOrder.Status.COMPLETED and not request.user.can_manage_returns:
        raise PermissionDenied
    try:
        cancel_repair_order(
            order, by=request.user, reason=request.POST.get("reason", ""),
            author=request.POST.get("author", ""),
        )
    except RepairError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Заказ {order.number} отменён.")
    return redirect("repair_order_detail", pk=pk)


@login_required
def repair_order_cancel_confirm(request, pk):
    _require_repairs(request)
    order = get_object_or_404(RepairOrder, pk=pk)
    if order.status == RepairOrder.Status.COMPLETED and not request.user.can_manage_returns:
        raise PermissionDenied
    if order.status not in (RepairOrder.Status.DRAFT, RepairOrder.Status.COMPLETED):
        messages.error(request, "Этот заказ уже отменён.")
        return redirect("repair_order_detail", pk=pk)
    return render(
        request, "repairs/repair_order_cancel_confirm.html",
        {"order": order, "form": RepairCancellationForm()},
    )
