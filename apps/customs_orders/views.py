"""Read-only order pages and the signed boundary selection workflow."""
from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_safe

from apps.actions.views import _require_access

from .export import export_customs_order_xlsx
from .models import CustomsOrder, CustomsOrderLine
from .services import (
    CustomsOrderError,
    create_customs_order_from_boundary,
    current_fx_rate,
    eligible_customs_sources,
    next_order_number,
    selection_payload,
)


def _require_customs_access(request):
    _require_access(request)
    if not request.user.can_view_purchase_cost:
        raise PermissionDenied


@login_required
@require_safe
def customs_orders_list(request):
    _require_customs_access(request)
    return render(request, "customs_orders/list.html", {
        "orders": CustomsOrder.objects.select_related("created_by"),
        "initialization_required": not CustomsOrder.objects.exists(),
    })


@login_required
@require_safe
def customs_order_detail(request, pk):
    _require_customs_access(request)
    order = get_object_or_404(CustomsOrder.objects.prefetch_related("lines"), pk=pk)
    if request.GET.get("export") == "1":
        output = export_customs_order_xlsx(order)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="customs_order_{order.number}.xlsx"'
        )
        return response
    return render(request, "customs_orders/detail.html", {"order": order})


def _selection_previews(sources, rate):
    """Exact display totals; the service independently recalculates on finalization."""
    quantity = Decimal("0")
    amount = Decimal("0")
    complete = True
    previews = []
    labels = dict(CustomsOrderLine.Source.choices)
    for index, row in enumerate(sources, start=1):
        row["source_label"] = labels[row["source"]]
        quantity += row["quantity"]
        if row["usd_price"] is None:
            complete = False
        else:
            amount += (row["usd_price"] * row["quantity"] * rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP,
            )
        previews.append({
            "count": index,
            "quantity": format(quantity, ".3f"),
            "amount": format(amount, ".2f") if complete else None,
        })
    return previews


@login_required
@require_http_methods(["GET", "POST"])
def customs_order_selection(request):
    _require_customs_access(request)
    order_type = request.POST.get("order_type") if request.method == "POST" else request.GET.get(
        "order_type", CustomsOrder.OrderType.ORIGINAL
    )
    if order_type not in CustomsOrder.OrderType.values:
        order_type = CustomsOrder.OrderType.ORIGINAL
    if request.method == "POST":
        try:
            kind, source_id = request.POST.get("boundary", "").split(":", 1)
            number = int(request.POST.get("number", ""))
            boundary_source = (kind, int(source_id))
        except (ValueError, TypeError):
            messages.error(request, "Выберите крайнюю деталь и введите положительный номер заказа.")
        else:
            try:
                order = create_customs_order_from_boundary(
                    number=number, boundary_source=boundary_source,
                    selection_token=request.POST.get("selection_token", ""), order_type=order_type,
                    by=request.user,
                )
            except CustomsOrderError as exc:
                messages.error(request, str(exc))
            else:
                return redirect("customs_order_detail", pk=order.pk)
    sources = eligible_customs_sources(order_type)
    rate = current_fx_rate()
    return render(request, "customs_orders/select.html", {
        "sources": sources,
        "selection_token": selection_payload(sources, order_type=order_type),
        "selection_previews": _selection_previews(sources, rate),
        "fx_rate": rate,
        "order_type": order_type,
        "order_type_choices": CustomsOrder.OrderType.choices,
        "next_order_number": next_order_number(order_type),
        "initialization_required": not CustomsOrder.objects.exists(),
    })
