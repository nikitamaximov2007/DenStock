from django.contrib import admin

from .models import InventoryCountDocument, InventoryCountLine


class InventoryCountLineInline(admin.TabularInline):
    model = InventoryCountLine
    extra = 0
    autocomplete_fields = ["stock_lot", "part_type", "location"]
    readonly_fields = ("expected_quantity", "unit_cost_rub", "adjustment")


@admin.register(InventoryCountDocument)
class InventoryCountDocumentAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "scope_location", "completed_at")
    list_filter = ("status",)
    search_fields = ("number", "comment")
    readonly_fields = ("number", "created_at", "updated_at", "completed_at", "canceled_at")
    inlines = [InventoryCountLineInline]
