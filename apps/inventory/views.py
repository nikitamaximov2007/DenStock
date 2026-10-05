from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.catalog.models import PartType
from apps.core.forms import ImageUploadForm
from apps.core.images import add_image, deactivate_image, set_primary
from apps.procurement.models import Batch, BatchLine
from apps.warehouse.models import StorageLocation
from apps.warehouse.services import attach_movement_location_history

from .forms import (
    AdjustLotForm,
    MoveItemForm,
    MoveLotForm,
    PartItemBulkForm,
    PartItemCreateForm,
    PartItemEditForm,
    StockLotCreateForm,
    StockLotEditForm,
    StockLotQuickForm,
)
from .models import PartItem, PartItemImage, StockBalance, StockLot, StockMovement
from .movement import live_stock_rows
from .presentation import (
    attach_movement_identity,
    attach_part_identity,
    identity_numbers_prefetch,
    part_exact_number,
    with_part_identity,
)
from .services import (
    ITEM_PHYSICAL_STATUSES,
    LOT_EDIT_REFUSED,
    LOT_PHYSICAL_STATUSES,
    RECEIVE_HISTORY_UNPROVEN,
    InventoryError,
    adjust_stock_lot_quantity,
    change_part_item_status,
    change_stock_lot_status,
    create_part_items,
    create_stock_lot,
    lot_is_directly_editable,
    move_part_item,
    move_stock_lot,
    receive_part_item,
    receive_stock_lot,
    received_quantity,
    remaining_qty,
    update_part_item,
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


class PartItemListView(InventoryViewMixin, ListView):
    model = PartItem
    template_name = "inventory/item_list.html"
    context_object_name = "items"
    paginate_by = 50

    def get_queryset(self):
        qs = with_part_identity(
            PartItem.objects.select_related("part_type", "batch", "current_location")
        )
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
        attach_part_identity(ctx["items"])  # exact-артикул отдельной колонкой
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
        ctx["part_exact_number"] = part_exact_number(self.object.part_type, default="")
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        ctx["can_manage"] = self.request.user.can_manage_inventory
        ctx["can_print_labels"] = self.request.user.can_print_labels
        ctx["can_manage_images"] = self.request.user.can_manage_images
        allowed = self.object.ALLOWED_TRANSITIONS.get(self.object.status, [])
        if self.object.status == PartItem.Status.RECEIVING:
            # «Принять в ячейку» рядом уже есть переход в «Доступен»; вторая
            # кнопка с тем же действием только путала бы.
            allowed = [s for s in allowed if s != PartItem.Status.AVAILABLE]
        ctx["next_statuses"] = [(s, PartItem.Status(s).label) for s in allowed]
        ctx["movements"] = list(self.object.movements.select_related(
            "from_location", "to_location", "created_by"
        )[:10])
        attach_movement_location_history(ctx["movements"])
        images = list(self.object.images.filter(is_active=True))
        ctx["images"] = images
        primary = next((i for i in images if i.is_primary), None)
        # Если у экземпляра своих фото нет — показываем типовое фото вида (с пометкой).
        if primary is None:
            primary = self.object.part_type.images.filter(
                is_active=True, is_primary=True
            ).first()
            ctx["primary_is_type_fallback"] = primary is not None
        ctx["primary_image"] = primary
        if (
            self.request.user.can_manage_inventory
            and self.object.status == PartItem.Status.RECEIVING
        ):
            ctx["receive_locations"] = StorageLocation.objects.filter(
                storage_allowed=True, is_active=True
            )
        return ctx


@login_required
def item_edit(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    item = get_object_or_404(PartItem, pk=pk)
    if request.method == "POST":
        form = PartItemEditForm(request.POST)
        if form.is_valid():
            try:
                update_part_item(
                    item,
                    serial_number=form.cleaned_data["serial_number"],
                    current_location=form.cleaned_data["current_location"],
                    note=form.cleaned_data["note"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Экземпляр сохранён.")
                return redirect("item_detail", pk=item.pk)
    else:
        form = PartItemEditForm(initial={
            "serial_number": item.serial_number,
            "current_location": item.current_location_id,
            "note": item.note,
        })
    return render(
        request, "inventory/item_form.html",
        {"form": form, "item": item, "title": f"Экземпляр {item.internal_number}"},
    )


@login_required
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
        {"form": form, "line": line, "title": f"Экземпляр - {line.part_type}"},
    )


@login_required
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
        {"form": form, "line": line, "title": f"Массовое создание - {line.part_type}"},
    )


@login_required
@require_POST
def item_status_change(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    item = get_object_or_404(PartItem, pk=pk)
    try:
        item = change_part_item_status(item, request.POST.get("status", ""), by=request.user)
    except InventoryError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Статус экземпляра: {item.get_status_display()}.")
    return redirect("item_detail", pk=pk)


# --- Слой 24: фотографии экземпляра (информационный слой, без складской физики) ---


def _require_images(request) -> None:
    if not request.user.can_manage_images:
        raise PermissionDenied


@login_required
@require_POST
def item_image_add(request, pk):
    _require_images(request)
    item = get_object_or_404(PartItem, pk=pk)
    form = ImageUploadForm(request.POST, request.FILES)
    if form.is_valid():
        add_image(
            item.images, image=form.cleaned_data["image"],
            caption=form.cleaned_data["caption"], by=request.user,
        )
        messages.success(request, "Фото добавлено.")
    else:
        messages.error(request, "; ".join(form.errors.get("image", ["Не удалось загрузить фото."])))
    return redirect("item_detail", pk=pk)


@login_required
@require_POST
def item_image_primary(request, pk):
    _require_images(request)
    image = get_object_or_404(PartItemImage, pk=pk)
    set_primary(image)
    messages.success(request, "Главное фото обновлено.")
    return redirect("item_detail", pk=image.part_item_id)


@login_required
@require_POST
def item_image_delete(request, pk):
    _require_images(request)
    image = get_object_or_404(PartItemImage, pk=pk)
    item_pk = image.part_item_id
    deactivate_image(image)
    messages.success(request, "Фото удалено.")
    return redirect("item_detail", pk=item_pk)


# --- Количественные лоты (StockLot) -----------------------------------------


class StockLotListView(InventoryViewMixin, ListView):
    model = StockLot
    template_name = "inventory/lot_list.html"
    context_object_name = "lots"
    paginate_by = 50

    def get_queryset(self):
        qs = with_part_identity(
            StockLot.objects.select_related("part_type", "batch", "location")
        )
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
        attach_part_identity(ctx["lots"])  # exact-артикул отдельной колонкой
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
        ctx["part_exact_number"] = part_exact_number(self.object.part_type, default="")
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        ctx["can_manage"] = self.request.user.can_manage_inventory
        ctx["can_stocktake"] = self.request.user.can_manage_stocktaking
        ctx["lot_editable"] = lot_is_directly_editable(self.object)
        allowed = self.object.ALLOWED_TRANSITIONS.get(self.object.status, [])
        if self.object.status == StockLot.Status.RECEIVING:
            # «Принять» рядом и есть переход в «Доступен»; вторая кнопка с тем
            # же действием только путала бы.
            allowed = [s for s in allowed if s != StockLot.Status.AVAILABLE]
        ctx["next_statuses"] = [(s, StockLot.Status(s).label) for s in allowed]
        ctx["movements"] = list(self.object.movements.select_related(
            "from_location", "to_location", "created_by"
        )[:10])
        attach_movement_location_history(ctx["movements"])
        return ctx


@login_required
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
         "title": f"Лот - {line.part_type}"},
    )


@login_required
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
                received, proven = received_quantity(line)
                if not proven:
                    raise InventoryError(RECEIVE_HISTORY_UNPROVEN)
                create_stock_lot(
                    line, form.cleaned_data["location"],
                    max(line.quantity - received, 0),
                    note=form.cleaned_data["note"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Лот на остаток создан.")
            return redirect("batch_detail", pk=line.batch_id)
    else:
        form = StockLotQuickForm()
    received, proven = received_quantity(line)
    return render(
        request, "inventory/lot_form.html",
        {
            "form": form,
            "line": line,
            "remaining": max(line.quantity - received, 0) if proven else None,
            "provenance_error": None if proven else RECEIVE_HISTORY_UNPROVEN,
            "title": f"Лот на остаток - {line.part_type}",
        },
    )


@login_required
def lot_edit(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    lot = get_object_or_404(StockLot, pk=pk)
    if not lot_is_directly_editable(lot):
        messages.error(request, LOT_EDIT_REFUSED)
        return redirect("lot_detail", pk=lot.pk)
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


@login_required
@require_POST
def lot_status_change(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    lot = get_object_or_404(StockLot, pk=pk)
    try:
        lot = change_stock_lot_status(lot, request.POST.get("status", ""), by=request.user)
    except InventoryError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Статус лота: {lot.get_status_display()}.")
    return redirect("lot_detail", pk=pk)


# --- Слой 10: журнал движений, остатки, приёмка/перемещение/корректировка ----


class MovementListView(InventoryViewMixin, ListView):
    model = StockMovement
    template_name = "inventory/movement_list.html"
    context_object_name = "movements"
    paginate_by = 50

    def get_queryset(self):
        qs = StockMovement.objects.select_related(
            "part_type", "stock_lot", "part_item", "batch",
            "from_location", "to_location", "created_by",
            "part_type__brp_link__brp_part", "part_type__polaris_link__polaris_part",
        ).prefetch_related(identity_numbers_prefetch())
        mtype = self.request.GET.get("type")
        part = self.request.GET.get("part")
        batch = self.request.GET.get("batch")
        location = self.request.GET.get("location")
        if mtype:
            qs = qs.filter(movement_type=mtype)
        if part:
            qs = qs.filter(part_type_id=part)
        if batch:
            qs = qs.filter(batch_id=batch)
        if location:
            qs = qs.filter(Q(from_location_id=location) | Q(to_location_id=location))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        attach_movement_identity(ctx["movements"])
        attach_movement_location_history(ctx["movements"])
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        return ctx


class MovementDetailView(InventoryViewMixin, DetailView):
    model = StockMovement
    template_name = "inventory/movement_detail.html"
    context_object_name = "movement"

    def get_queryset(self):
        return StockMovement.objects.select_related(
            "part_type", "stock_lot", "part_item", "batch", "batch_line",
            "from_location", "to_location", "created_by",
            "part_type__brp_link__brp_part", "part_type__polaris_link__polaris_part",
        ).prefetch_related(identity_numbers_prefetch())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        attach_movement_identity([ctx["movement"]])
        attach_movement_location_history([ctx["movement"]])
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        return ctx


class BalanceListView(InventoryViewMixin, ListView):
    model = StockBalance
    template_name = "inventory/balance_list.html"
    context_object_name = "balances"
    paginate_by = 50

    def get_queryset(self):
        return live_stock_rows(
            part_id=self.request.GET.get("part") or None,
            batch_id=self.request.GET.get("batch") or None,
            location_id=self.request.GET.get("location") or None,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parts"] = PartType.objects.filter(
            Q(stock_lots__status__in=LOT_PHYSICAL_STATUSES, stock_lots__quantity__gt=0)
            | Q(
                items__status__in=ITEM_PHYSICAL_STATUSES,
                items__current_location__isnull=False,
            )
        ).distinct()
        ctx["batches"] = Batch.objects.filter(
            Q(stock_lots__status__in=LOT_PHYSICAL_STATUSES, stock_lots__quantity__gt=0)
            | Q(
                items__status__in=ITEM_PHYSICAL_STATUSES,
                items__current_location__isnull=False,
            )
        ).distinct()
        ctx["locations"] = StorageLocation.objects.filter(
            Q(stock_lots__status__in=LOT_PHYSICAL_STATUSES, stock_lots__quantity__gt=0)
            | Q(items__status__in=ITEM_PHYSICAL_STATUSES)
        ).distinct()
        ctx["f_part"] = self.request.GET.get("part", "")
        ctx["f_batch"] = self.request.GET.get("batch", "")
        ctx["f_location"] = self.request.GET.get("location", "")
        return ctx


@login_required
@require_POST
def item_receive(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    item = get_object_or_404(PartItem, pk=pk)
    loc_id = request.POST.get("to_location")
    to_location = get_object_or_404(StorageLocation, pk=loc_id) if loc_id else None
    try:
        receive_part_item(item, to_location=to_location, by=request.user)
    except InventoryError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Экземпляр принят в ячейку.")
    return redirect("item_detail", pk=pk)


@login_required
def item_move(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    item = get_object_or_404(PartItem, pk=pk)
    if request.method == "POST":
        form = MoveItemForm(request.POST)
        if form.is_valid():
            try:
                move_part_item(
                    item, form.cleaned_data["to_location"],
                    by=request.user, comment=form.cleaned_data["comment"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Экземпляр перемещён.")
                return redirect("item_detail", pk=pk)
    else:
        form = MoveItemForm()
    return render(
        request, "inventory/move_form.html",
        {"form": form, "title": f"Переместить экземпляр {item.internal_number}",
         "back_url": reverse("item_detail", args=[pk])},
    )


@login_required
@require_POST
def lot_receive(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    lot = get_object_or_404(StockLot, pk=pk)
    try:
        receive_stock_lot(lot, by=request.user)
    except InventoryError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Лот принят.")
    return redirect("lot_detail", pk=pk)


@login_required
def lot_move(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    lot = get_object_or_404(StockLot, pk=pk)
    if request.method == "POST":
        form = MoveLotForm(request.POST)
        if form.is_valid():
            try:
                move_stock_lot(
                    lot, form.cleaned_data["to_location"],
                    by=request.user, comment=form.cleaned_data["comment"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Лот перемещён.")
                return redirect("lot_detail", pk=pk)
    else:
        form = MoveLotForm()
    return render(
        request, "inventory/move_form.html",
        {"form": form, "title": f"Переместить лот {lot.pk}",
         "back_url": reverse("lot_detail", args=[pk])},
    )


@login_required
def lot_adjust(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    lot = get_object_or_404(StockLot, pk=pk)
    if request.method == "POST":
        form = AdjustLotForm(request.POST)
        if form.is_valid():
            try:
                adjust_stock_lot_quantity(
                    lot, form.cleaned_data["delta"],
                    by=request.user, comment=form.cleaned_data["comment"],
                )
            except InventoryError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Количество скорректировано.")
                return redirect("lot_detail", pk=pk)
    else:
        form = AdjustLotForm()
    return render(
        request, "inventory/adjust_form.html",
        {"form": form, "lot": lot, "title": f"Корректировка лота {lot.pk}",
         "back_url": reverse("lot_detail", args=[pk])},
    )
