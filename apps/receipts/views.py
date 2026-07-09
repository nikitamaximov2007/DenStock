"""Layer 28 — экраны поступления. View — оркестратор (как в writeoffs).

Любая мутация документа/склада идёт через `apps.receipts.services`; вьюхи сами
в StockMovement/StockBalance/лоты/экземпляры не пишут. Доступ — по
can_manage_inventory (Администратор/Руководитель/Кладовщик), как у приёмки.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.inventory.models import PartItem, StockLot, StockMovement

from .forms import ReceiptForm, ReceiptLineForm
from .models import Receipt, ReceiptLine
from .services import (
    ReceiptError,
    add_line,
    create_receipt,
    post_receipt,
    receipt_totals,
    remove_line,
    update_line,
    update_receipt,
)

_COST = DecimalField(max_digits=20, decimal_places=2)


def _require_manage(request) -> None:
    if not request.user.can_manage_inventory:
        raise PermissionDenied


@login_required
def receipt_list(request):
    _require_manage(request)
    status = request.GET.get("status", "")
    q = (request.GET.get("q") or "").strip()
    # Layer 34: документы, созданные пересчётом ячейки, - технические
    # (первичный ввод, раздел «Инвентаризация»). «Поступления» показывают
    # только реальные поставки; сами документы не удаляются: на них
    # ссылаются партии/лоты/движения.
    qs = Receipt.objects.select_related("supplier", "created_by", "posted_by").filter(
        counting_session__isnull=True
    )
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(
            Q(number__icontains=q) | Q(supplier__name__icontains=q) | Q(comment__icontains=q)
        )
    line_cost = ExpressionWrapper(
        F("lines__quantity") * F("lines__unit_cost_rub"), output_field=_COST
    )
    qs = qs.annotate(line_count=Count("lines"), cost_total=Sum(line_cost))
    return render(
        request,
        "receipts/receipt_list.html",
        {
            "receipts": qs.order_by("-created_at")[:100],
            "status": status,
            "q": q,
            "statuses": Receipt.Status.choices,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def receipt_create(request):
    _require_manage(request)
    if request.method == "POST":
        form = ReceiptForm(request.POST)
        if form.is_valid():
            receipt = create_receipt(
                supplier=form.cleaned_data["supplier"],
                received_at=form.cleaned_data["received_at"],
                comment=form.cleaned_data["comment"],
                by=request.user,
            )
            messages.success(request, f"Черновик поступления {receipt.number} создан.")
            return redirect("receipt_detail", pk=receipt.pk)
    else:
        form = ReceiptForm()
    return render(
        request,
        "receipts/receipt_form.html",
        {"form": form, "title": "Новое поступление", "submit_label": "Создать черновик"},
    )


@login_required
def receipt_detail(request, pk):
    _require_manage(request)
    receipt = get_object_or_404(
        Receipt.objects.select_related("supplier", "created_by", "posted_by", "batch"), pk=pk
    )
    lines = receipt.lines.select_related("part_type", "location", "batch_line")
    ctx = {
        "receipt": receipt,
        "lines": lines,
        "totals": receipt_totals(receipt),
        "is_draft": receipt.is_draft,
        "can_manage_parts": request.user.can_manage_parts,
        "show_costs": request.user.can_view_purchase_cost,
        # 33.1: документ из пересчёта ячейки - первичный ввод, его "цена" -
        # оценка по цене клиента, а не себестоимость закупки.
        "is_counting_receipt": receipt.counting_session.exists(),
    }
    if receipt.is_draft:
        initial = {}
        new_part = request.GET.get("new_part")
        if new_part and new_part.isdigit():
            # Возврат из «+ Новая деталь»: подставляем созданную деталь в форму.
            initial = {"part_type": int(new_part), "quantity": Decimal("1")}
        ctx["line_form"] = ReceiptLineForm(initial=initial)
    else:
        batch = receipt.batch
        ctx["created_items"] = PartItem.objects.filter(batch=batch).select_related(
            "part_type", "current_location"
        )
        ctx["created_lots"] = StockLot.objects.filter(batch=batch).select_related(
            "part_type", "location"
        )
        ctx["movement_count"] = StockMovement.objects.filter(batch=batch).count()
    return render(request, "receipts/receipt_detail.html", ctx)


@login_required
def receipt_edit(request, pk):
    """Правка шапки черновика (поставщик/дата/комментарий)."""
    _require_manage(request)
    receipt = get_object_or_404(Receipt, pk=pk)
    if not receipt.is_draft:
        messages.error(request, "Проведённое поступление изменять нельзя.")
        return redirect("receipt_detail", pk=pk)
    if request.method == "POST":
        form = ReceiptForm(request.POST, instance=receipt)
        if form.is_valid():
            update_receipt(
                receipt,
                supplier=form.cleaned_data["supplier"],
                received_at=form.cleaned_data["received_at"],
                comment=form.cleaned_data["comment"],
            )
            messages.success(request, "Шапка поступления обновлена.")
            return redirect("receipt_detail", pk=pk)
    else:
        form = ReceiptForm(instance=receipt)
    return render(
        request,
        "receipts/receipt_form.html",
        {
            "form": form,
            "title": f"Поступление {receipt.number}: шапка",
            "submit_label": "Сохранить",
            "back_url_pk": receipt.pk,
        },
    )


@login_required
@require_POST
def receipt_add_line(request, pk):
    _require_manage(request)
    receipt = get_object_or_404(Receipt, pk=pk)
    form = ReceiptLineForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте позицию: деталь, количество, цену и ячейку.")
        return redirect("receipt_detail", pk=pk)
    try:
        line = add_line(
            receipt,
            part_type=form.cleaned_data["part_type"],
            quantity=form.cleaned_data["quantity"],
            unit_cost_rub=form.cleaned_data["unit_cost_rub"],
            location=form.cleaned_data["location"],
            comment=form.cleaned_data["comment"],
        )
    except ReceiptError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Позиция добавлена: {line}.")
    return redirect("receipt_detail", pk=pk)


@login_required
def receipt_line_edit(request, pk):
    _require_manage(request)
    line = get_object_or_404(ReceiptLine.objects.select_related("receipt"), pk=pk)
    if not line.receipt.is_draft:
        messages.error(request, "Проведённое поступление изменять нельзя.")
        return redirect("receipt_detail", pk=line.receipt_id)
    if request.method == "POST":
        form = ReceiptLineForm(request.POST, instance=line)
        if form.is_valid():
            try:
                update_line(
                    line,
                    part_type=form.cleaned_data["part_type"],
                    quantity=form.cleaned_data["quantity"],
                    unit_cost_rub=form.cleaned_data["unit_cost_rub"],
                    location=form.cleaned_data["location"],
                    comment=form.cleaned_data["comment"],
                )
            except ReceiptError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Позиция обновлена.")
                return redirect("receipt_detail", pk=line.receipt_id)
    else:
        form = ReceiptLineForm(instance=line)
    return render(
        request,
        "receipts/receipt_form.html",
        {
            "form": form,
            "title": f"Позиция поступления {line.receipt.number}",
            "submit_label": "Сохранить",
            "back_url_pk": line.receipt_id,
        },
    )


@login_required
@require_POST
def receipt_remove_line(request, pk):
    _require_manage(request)
    line = get_object_or_404(ReceiptLine, pk=pk)
    receipt_pk = line.receipt_id
    try:
        remove_line(line)
    except ReceiptError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Позиция удалена из черновика.")
    return redirect("receipt_detail", pk=receipt_pk)


@login_required
@require_POST
def receipt_post(request, pk):
    _require_manage(request)
    receipt = get_object_or_404(Receipt, pk=pk)
    try:
        post_receipt(receipt, by=request.user)
    except ReceiptError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Поступление {receipt.number} проведено: остатки и движения созданы.",
        )
    return redirect("receipt_detail", pk=pk)
