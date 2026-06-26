from django.contrib import admin

from .models import StockReturn, StockReturnLine


class StockReturnLineInline(admin.TabularInline):
    model = StockReturnLine
    extra = 0
    autocomplete_fields = ["part_type", "part_item", "stock_lot", "to_location"]
    readonly_fields = ("unit_cost_rub", "total_cost_rub", "returned_lot")


@admin.register(StockReturn)
class StockReturnAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "source_type", "source_id", "cost_total", "completed_at")
    list_filter = ("status", "source_type")
    search_fields = ("number", "reason", "comment")
    readonly_fields = ("number", "created_at", "updated_at", "completed_at", "cost_total")
    inlines = [StockReturnLineInline]
