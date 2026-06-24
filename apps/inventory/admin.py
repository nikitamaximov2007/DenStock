from django.contrib import admin

from .models import NumberSequence, PartItem, StockLot


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
