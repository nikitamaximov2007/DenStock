from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, UpdateView

from apps.catalog.models import PartType
from apps.procurement.models import Batch, BatchLine
from apps.warehouse.models import StorageLocation

from .forms import (
    PartItemBulkForm,
    PartItemCreateForm,
    PartItemEditForm,
    StockLotCreateForm,
    StockLotEditForm,
    StockLotQuickForm,
)
from .models import PartItem, StockLot
from .services import (
    InventoryError,
    create_part_items,
    create_stock_lot,
    remaining_qty,
    update_stock_lot,
)


def _can_view_inventory(user) -> bool:
    # Управляющие инвентарём + наблюдатель (только чтение). Продавец/Мастер — нет.
    return user.can_manage_inventory or user.is_viewer


class InventoryViewMixin:
    """Доступ к разделу экземпляров для просмотра."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not _can_view_inventory(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class InventoryManageMixin:
    """Доступ к управлению экземплярами (создание/правка)."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.can_manage_inventory:
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class PartItemListView(InventoryViewMixin, ListView):
    model = PartItem
    template_name = "inventory/item_list.html"
    context_object_name = "items"
    paginate_by = 50

    def get_queryset(self):
        qs = PartItem.objects.select_related("part_type", "batch", "current_location")
        status = self.request.GET.get("status")
        part = self.request.GET.get("part")
        batch = self.request.GET.get("batch")
        location = self.request.GET.get("location")
        if status:
            qs = qs.filter(status=status)
        if part:
            qs = qs.filter(part_type_id=part)
        if batch:
            qs = qs.filter(batch_id=batch)
        if location:
            qs = qs.filter(current_location_id=location)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        ctx["can_manage"] = self.request.user.can_manage_inventory
        ctx["statuses"] = PartItem.Status.choices
        ctx["parts"] = PartType.objects.filter(items__isnull=False).distinct()
        ctx["batches"] = Batch.objects.filter(items__isnull=False).distinct()
        ctx["locations"] = StorageLocation.objects.filter(items__isnull=False).distinct()
        ctx["f_status"] = self.request.GET.get("status", "")
        ctx["f_part"] = self.request.GET.get("part", "")
        ctx["f_batch"] = self.request.GET.get("batch", "")
        ctx["f_location"] = self.request.GET.get("location", "")
        return ctx


class PartItemDetailView(InventoryViewMixin, DetailView):
    model = PartItem
    template_name = "inventory/item_detail.html"
    context_object_name = "item"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        ctx["can_manage"] = self.request.user.can_manage_inventory
        allowed = self.object.ALLOWED_TRANSITIONS.get(self.object.status, [])
        ctx["next_statuses"] = [(s, PartItem.Status(s).label) for s in allowed]
        return ctx


class PartItemUpdateView(InventoryManageMixin, UpdateView):
    model = PartItem
    form_class = PartItemEditForm
    template_name = "inventory/item_form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"Экземпляр {self.object.internal_number}"
        return ctx

    def form_valid(self, form):
        messages.success(self.request, "Экземпляр сохранён.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("item_detail", args=[self.object.pk])


def item_create(request, line_pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    line = get_object_or_404(
        BatchLine.objects.select_related("batch", "part_type"), pk=line_pk
    )
    if request.method == "POST":
        form = PartItemCreateForm(request.POST)
        if form.is_valid():
            try:
                create_part_items(
                    line, 1,
                    serial_number=form.cleaned_data["serial_number"],
                    current_location=form.cleaned_data["current_location"],
                    note=form.cleaned_data["note"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Экземпляр создан.")
            return redirect("batch_detail", pk=line.batch_id)
    else:
        form = PartItemCreateForm()
    return render(
        request, "inventory/item_form.html",
        {"form": form, "line": line, "title": f"Экземпляр — {line.part_type}"},
    )


def item_bulk_create(request, line_pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    line = get_object_or_404(
        BatchLine.objects.select_related("batch", "part_type"), pk=line_pk
    )
    if request.method == "POST":
        form = PartItemBulkForm(request.POST)
        if form.is_valid():
            try:
                created = create_part_items(
                    line, form.cleaned_data["count"], note=form.cleaned_data["note"]
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Создано экземпляров: {len(created)}.")
            return redirect("batch_detail", pk=line.batch_id)
    else:
        form = PartItemBulkForm()
    return render(
        request, "inventory/item_bulk_form.html",
        {"form": form, "line": line, "title": f"Массовое создание — {line.part_type}"},
    )


@require_POST
def item_status_change(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    item = get_object_or_404(PartItem, pk=pk)
    new_status = request.POST.get("status", "")
    if item.can_transition_to(new_status):
        item.status = new_status
        item.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Статус экземпляра: {item.get_status_display()}.")
    else:
        messages.error(request, "Недопустимый переход статуса.")
    return redirect("item_detail", pk=pk)


# --- Количественные лоты (StockLot) -----------------------------------------


class StockLotListView(InventoryViewMixin, ListView):
    model = StockLot
    template_name = "inventory/lot_list.html"
    context_object_name = "lots"
    paginate_by = 50

    def get_queryset(self):
        qs = StockLot.objects.select_related("part_type", "batch", "location")
        status = self.request.GET.get("status")
        part = self.request.GET.get("part")
        batch = self.request.GET.get("batch")
        location = self.request.GET.get("location")
        if status:
            qs = qs.filter(status=status)
        if part:
            qs = qs.filter(part_type_id=part)
        if batch:
            qs = qs.filter(batch_id=batch)
        if location:
            qs = qs.filter(location_id=location)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        ctx["can_manage"] = self.request.user.can_manage_inventory
        ctx["statuses"] = StockLot.Status.choices
        ctx["parts"] = PartType.objects.filter(stock_lots__isnull=False).distinct()
        ctx["batches"] = Batch.objects.filter(stock_lots__isnull=False).distinct()
        ctx["locations"] = StorageLocation.objects.filter(stock_lots__isnull=False).distinct()
        ctx["f_status"] = self.request.GET.get("status", "")
        ctx["f_part"] = self.request.GET.get("part", "")
        ctx["f_batch"] = self.request.GET.get("batch", "")
        ctx["f_location"] = self.request.GET.get("location", "")
        return ctx


class StockLotDetailView(InventoryViewMixin, DetailView):
    model = StockLot
    template_name = "inventory/lot_detail.html"
    context_object_name = "lot"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        ctx["can_manage"] = self.request.user.can_manage_inventory
        allowed = self.object.ALLOWED_TRANSITIONS.get(self.object.status, [])
        ctx["next_statuses"] = [(s, StockLot.Status(s).label) for s in allowed]
        return ctx


def lot_create(request, line_pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    line = get_object_or_404(
        BatchLine.objects.select_related("batch", "part_type"), pk=line_pk
    )
    if request.method == "POST":
        form = StockLotCreateForm(request.POST)
        if form.is_valid():
            try:
                create_stock_lot(
                    line, form.cleaned_data["location"], form.cleaned_data["quantity"],
                    note=form.cleaned_data["note"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Лот создан.")
            return redirect("batch_detail", pk=line.batch_id)
    else:
        form = StockLotCreateForm()
    return render(
        request, "inventory/lot_form.html",
        {"form": form, "line": line, "remaining": remaining_qty(line),
         "title": f"Лот — {line.part_type}"},
    )


def lot_create_remaining(request, line_pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    line = get_object_or_404(
        BatchLine.objects.select_related("batch", "part_type"), pk=line_pk
    )
    if request.method == "POST":
        form = StockLotQuickForm(request.POST)
        if form.is_valid():
            try:
                create_stock_lot(
                    line, form.cleaned_data["location"], remaining_qty(line),
                    note=form.cleaned_data["note"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Лот на остаток создан.")
            return redirect("batch_detail", pk=line.batch_id)
    else:
        form = StockLotQuickForm()
    return render(
        request, "inventory/lot_form.html",
        {"form": form, "line": line, "remaining": remaining_qty(line),
         "title": f"Лот на остаток — {line.part_type}"},
    )


def lot_edit(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    lot = get_object_or_404(StockLot, pk=pk)
    if request.method == "POST":
        form = StockLotEditForm(request.POST)
        if form.is_valid():
            try:
                update_stock_lot(
                    lot, location=form.cleaned_data["location"],
                    quantity=form.cleaned_data["quantity"], note=form.cleaned_data["note"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Лот сохранён.")
                return redirect("lot_detail", pk=lot.pk)
    else:
        form = StockLotEditForm(
            initial={"location": lot.location_id, "quantity": lot.quantity, "note": lot.note}
        )
    return render(
        request, "inventory/lot_form.html",
        {"form": form, "lot": lot, "title": f"Лот {lot.pk}"},
    )


@require_POST
def lot_status_change(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    lot = get_object_or_404(StockLot, pk=pk)
    new_status = request.POST.get("status", "")
    if lot.can_transition_to(new_status):
        lot.status = new_status
        lot.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Статус лота: {lot.get_status_display()}.")
    else:
        messages.error(request, "Недопустимый переход статуса.")
    return redirect("lot_detail", pk=pk)
