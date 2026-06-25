"""Слой 15 — экраны резервов. View — оркестратор.

Любая мутация остатка/брони идёт через `apps.sales.services`; вьюхи сами в
`StockBalance`/`StockMovement` не пишут. Hidden/query-параметры недоверенные:
объект всегда перечитывается из БД, права/статус/доступность проверяет сервис.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.inventory.models import PartItem

from .forms import (
    AddItemForm,
    AddLotForm,
    AddSaleItemForm,
    AddSaleLotForm,
    ReservationForm,
    SaleForm,
)
from .models import Reservation, ReservationLine, Sale, SaleLine
from .services import (
    ReservationError,
    SaleError,
    activate_reservation,
    add_part_item_to_reservation,
    add_part_item_to_sale,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    cancel_reservation,
    complete_sale,
    create_reservation,
    create_sale,
    create_sale_from_reservation,
    remove_reservation_line,
    remove_sale_line,
)


def _require_manage(request) -> None:
    if not request.user.can_manage_reservations:
        raise PermissionDenied


def _require_sales(request) -> None:
    if not request.user.can_manage_sales:
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
def reservation_list(request):
    status = request.GET.get("status", "")
    qs = Reservation.objects.select_related("created_by").order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    return render(
        request,
        "sales/reservation_list.html",
        {
            "reservations": qs[:100],
            "status": status,
            "statuses": Reservation.Status.choices,
            "can_manage": request.user.can_manage_reservations,
        },
    )


@login_required
def reservation_detail(request, pk):
    reservation = get_object_or_404(Reservation.objects.select_related("created_by"), pk=pk)
    lines = reservation.lines.select_related(
        "part_type",
        "part_item",
        "part_item__current_location",
        "stock_lot",
        "stock_lot__location",
    )
    is_open = reservation.status in (Reservation.Status.DRAFT, Reservation.Status.ACTIVE)
    return render(
        request,
        "sales/reservation_detail.html",
        {
            "reservation": reservation,
            "lines": lines,
            "can_manage": request.user.can_manage_reservations,
            "can_sell": request.user.can_manage_sales,
            "is_open": is_open,
            "show_costs": request.user.can_view_purchase_cost,
            "add_item_form": AddItemForm(),
            "add_lot_form": AddLotForm(),
        },
    )


@login_required
def reservation_create(request):
    _require_manage(request)
    if request.method == "POST":
        form = ReservationForm(request.POST)
        if form.is_valid():
            try:
                reservation = create_reservation(
                    customer_name=form.cleaned_data["customer_name"],
                    customer_phone=form.cleaned_data["customer_phone"],
                    comment=form.cleaned_data["comment"],
                    expires_at=form.cleaned_data["expires_at"],
                    by=request.user,
                )
            except ReservationError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Резерв {reservation.number} создан.")
                return redirect("reservation_detail", pk=reservation.pk)
    else:
        form = ReservationForm()
    return render(request, "sales/reservation_form.html", {"form": form})


@login_required
@require_POST
def reservation_add_item(request, pk):
    _require_manage(request)
    reservation = get_object_or_404(Reservation, pk=pk)
    item = _resolve_item(request.POST.get("code", ""))
    if item is None:
        messages.error(request, "Экземпляр по коду не найден.")
    else:
        try:
            add_part_item_to_reservation(reservation, item, by=request.user)
        except ReservationError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Экземпляр {item.internal_number} добавлен.")
    return redirect("reservation_detail", pk=pk)


@login_required
@require_POST
def reservation_add_lot(request, pk):
    _require_manage(request)
    reservation = get_object_or_404(Reservation, pk=pk)
    form = AddLotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте лот и количество.")
        return redirect("reservation_detail", pk=pk)
    try:
        add_stock_lot_to_reservation(
            reservation, form.cleaned_data["lot"], form.cleaned_data["quantity"], by=request.user
        )
    except ReservationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Количество из лота добавлено в резерв.")
    return redirect("reservation_detail", pk=pk)


@login_required
@require_POST
def reservation_remove_line(request, pk):
    _require_manage(request)
    line = get_object_or_404(ReservationLine, pk=pk)
    reservation_pk = line.reservation_id
    try:
        remove_reservation_line(line, by=request.user)
    except ReservationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Позиция снята с резерва.")
    return redirect("reservation_detail", pk=reservation_pk)


@login_required
@require_POST
def reservation_activate(request, pk):
    _require_manage(request)
    reservation = get_object_or_404(Reservation, pk=pk)
    try:
        activate_reservation(reservation, by=request.user)
    except ReservationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Резерв {reservation.number} активирован.")
    return redirect("reservation_detail", pk=pk)


@login_required
@require_POST
def reservation_cancel(request, pk):
    _require_manage(request)
    reservation = get_object_or_404(Reservation, pk=pk)
    try:
        cancel_reservation(reservation, by=request.user)
    except ReservationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Резерв {reservation.number} отменён.")
    return redirect("reservation_detail", pk=pk)


# --- Слой 16: продажи --------------------------------------------------------


@login_required
def sale_list(request):
    status = request.GET.get("status", "")
    qs = Sale.objects.select_related("sold_by").order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    return render(
        request,
        "sales/sale_list.html",
        {
            "sales": qs[:100],
            "status": status,
            "statuses": Sale.Status.choices,
            "can_sell": request.user.can_manage_sales,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def sale_detail(request, pk):
    sale = get_object_or_404(Sale.objects.select_related("sold_by", "reservation"), pk=pk)
    lines = sale.lines.select_related(
        "part_type",
        "part_item",
        "part_item__current_location",
        "stock_lot",
        "stock_lot__location",
    )
    is_draft = sale.status == Sale.Status.DRAFT
    return render(
        request,
        "sales/sale_detail.html",
        {
            "sale": sale,
            "lines": lines,
            "can_sell": request.user.can_manage_sales,
            "is_draft": is_draft,
            "show_costs": request.user.can_view_purchase_cost,
            "add_item_form": AddSaleItemForm(),
            "add_lot_form": AddSaleLotForm(),
        },
    )


@login_required
def sale_create(request):
    _require_sales(request)
    if request.method == "POST":
        form = SaleForm(request.POST)
        if form.is_valid():
            try:
                sale = create_sale(
                    customer_name=form.cleaned_data["customer_name"],
                    customer_phone=form.cleaned_data["customer_phone"],
                    comment=form.cleaned_data["comment"],
                    by=request.user,
                )
            except SaleError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Продажа {sale.number} создана.")
                return redirect("sale_detail", pk=sale.pk)
    else:
        form = SaleForm()
    return render(request, "sales/sale_form.html", {"form": form})


@login_required
@require_POST
def sale_add_item(request, pk):
    _require_sales(request)
    sale = get_object_or_404(Sale, pk=pk)
    form = AddSaleItemForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте код экземпляра и цену.")
        return redirect("sale_detail", pk=pk)
    item = _resolve_item(form.cleaned_data["code"])
    if item is None:
        messages.error(request, "Экземпляр по коду не найден.")
    else:
        try:
            add_part_item_to_sale(
                sale, item, unit_price=form.cleaned_data["unit_price"], by=request.user
            )
        except SaleError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Экземпляр {item.internal_number} добавлен.")
    return redirect("sale_detail", pk=pk)


@login_required
@require_POST
def sale_add_lot(request, pk):
    _require_sales(request)
    sale = get_object_or_404(Sale, pk=pk)
    form = AddSaleLotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте лот, количество и цену.")
        return redirect("sale_detail", pk=pk)
    try:
        add_stock_lot_to_sale(
            sale, form.cleaned_data["lot"], form.cleaned_data["quantity"],
            unit_price=form.cleaned_data["unit_price"], by=request.user,
        )
    except SaleError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Количество из лота добавлено в продажу.")
    return redirect("sale_detail", pk=pk)


@login_required
@require_POST
def sale_remove_line(request, pk):
    _require_sales(request)
    line = get_object_or_404(SaleLine, pk=pk)
    sale_pk = line.sale_id
    try:
        remove_sale_line(line, by=request.user)
    except SaleError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Позиция снята с продажи.")
    return redirect("sale_detail", pk=sale_pk)


@login_required
@require_POST
def sale_complete(request, pk):
    _require_sales(request)
    sale = get_object_or_404(Sale, pk=pk)
    try:
        complete_sale(sale, by=request.user)
    except SaleError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Продажа {sale.number} проведена.")
    return redirect("sale_detail", pk=pk)


@login_required
@require_POST
def sale_from_reservation(request, pk):
    _require_sales(request)
    reservation = get_object_or_404(Reservation, pk=pk)
    try:
        sale = create_sale_from_reservation(reservation, by=request.user)
    except SaleError as exc:
        messages.error(request, str(exc))
        return redirect("reservation_detail", pk=pk)
    messages.success(request, f"Создана продажа {sale.number} из резерва — проверьте цены.")
    return redirect("sale_detail", pk=sale.pk)
