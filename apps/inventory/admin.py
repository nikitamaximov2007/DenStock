from django.contrib import admin

from .models import NumberSequence, PartItem, StockBalance, StockLot, StockMovement


@admin.register(PartItem)
class PartItemAdmin(admin.ModelAdmin):
    list_display = (
        "internal_number", "part_type", "status", "serial_number",
        "current_location", "batch", "created_at",
    )
    list_filter = ("status", "part_type", "batch")
    search_fields = ("internal_number", "internal_barcode", "serial_number")
    readonly_fields = ("internal_number", "internal_barcode", "landed_cost_rub", "batch")


@admin.register(StockLot)
class StockLotAdmin(admin.ModelAdmin):
    list_display = (
        "id", "part_type", "location", "quantity", "status", "batch", "created_at",
    )
    list_filter = ("status", "part_type", "batch")
    search_fields = ("part_type__name", "location__code", "batch__number")
    readonly_fields = ("initial_quantity", "landed_unit_cost_rub", "batch")


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
