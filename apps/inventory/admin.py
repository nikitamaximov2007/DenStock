from django.contrib import admin

from .models import (
    NumberSequence,
    PartItem,
    PartPreferredLocation,
    StockBalance,
    StockLot,
    StockMovement,
)


@admin.register(PartItem)
class PartItemAdmin(admin.ModelAdmin):
    """Статус и место - складская физика, доступная только через сервисы
    (`receive_part_item`, `change_part_item_status`, `move_part_item`,
    `update_part_item`): напрямую в админке их менять нельзя, иначе это
    второй путь мимо журнала движений и кэша остатков. Серийник и примечание
    - не складская физика, их можно поправить и здесь.

    Экземпляр создаётся только `create_part_items` (из финансово закрытой
    строки партии, с номером из `NumberSequence`) - без него `internal_number`
    остался бы пустым, поэтому создание через админку выключено целиком.
    """

    list_display = (
        "internal_number", "part_type", "status", "serial_number",
        "current_location", "batch", "created_at",
    )
    list_filter = ("status", "part_type", "batch")
    search_fields = ("internal_number", "internal_barcode", "serial_number")
    readonly_fields = (
        "internal_number", "internal_barcode", "landed_cost_rub", "batch",
        "status", "current_location", "part_type", "batch_line",
    )

    def has_add_permission(self, request):
        return False


@admin.register(StockLot)
class StockLotAdmin(admin.ModelAdmin):
    """Количество, статус и ячейка - складская физика, доступная только через
    сервисы (`receive_stock_lot`, `change_stock_lot_status`, `move_stock_lot`,
    `adjust_stock_lot_quantity`, `update_stock_lot`): напрямую в админке их
    менять нельзя, иначе это второй путь мимо журнала движений и кэша
    остатков.

    Лот создаётся только `create_stock_lot` (из финансово закрытой строки
    партии, с проверкой лимита строки) - создание через админку выключено
    целиком.
    """

    list_display = (
        "id", "part_type", "location", "quantity", "status", "batch", "created_at",
    )
    list_filter = ("status", "part_type", "batch")
    search_fields = ("part_type__name", "location__code", "batch__number")
    # The part and batch line decide what the quantity means (pieces or a
    # measure): reassigning a fractional oil lot to a piece part here would
    # create an invalid piece balance, so the lot's identity is read-only too.
    readonly_fields = (
        "initial_quantity", "landed_unit_cost_rub", "batch", "origin_transfer",
        "quantity", "status", "location", "part_type", "batch_line",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # Deleting a lot bypasses the movement ledger and can erase stock or
        # reopen the lifetime receipt capacity of its supplier line.
        return False


@admin.register(NumberSequence)
class NumberSequenceAdmin(admin.ModelAdmin):
    list_display = ("key", "prefix", "last_value")
    readonly_fields = ("key", "prefix")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    """Журнал движений — append-only: только просмотр, без add/change/delete."""

    list_display = (
        "created_at", "movement_type", "part_type", "stock_lot", "part_item",
        "quantity", "from_location", "to_location", "created_by",
    )
    list_filter = ("movement_type", "part_type", "batch")
    search_fields = ("part_type__name", "comment")
    date_hierarchy = "created_at"
    readonly_fields = (
        "movement_type", "part_type", "part_item", "stock_lot", "batch", "batch_line",
        "from_location", "to_location", "quantity", "unit_cost_rub", "total_cost_rub",
        "created_by", "created_at", "document_type", "document_id", "comment",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockBalance)
class StockBalanceAdmin(admin.ModelAdmin):
    """Кэш остатков — read-only: пересобирается командой, руками не правится."""

    list_display = (
        "part_type", "location", "batch", "quantity_physical", "quantity_available",
        "quantity_quarantine", "updated_at",
    )
    list_filter = ("part_type", "location", "batch")
    search_fields = ("part_type__name", "location__code")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PartPreferredLocation)
class PartPreferredLocationAdmin(admin.ModelAdmin):
    """Просмотр подсказок размещения без ручного обхода складских сервисов."""

    list_display = ("part_type", "location", "updated_by", "updated_at")
    list_select_related = ("part_type", "location", "updated_by")
    search_fields = ("part_type__name", "location__code")
    readonly_fields = ("part_type", "location", "updated_by", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
