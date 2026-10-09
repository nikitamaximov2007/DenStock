from django.contrib import admin

from .models import StockReturn, StockReturnLine


def _is_posted(obj) -> bool:
    return obj is not None and obj.status != StockReturn.Status.DRAFT


class StockReturnLineInline(admin.TabularInline):
    model = StockReturnLine
    extra = 0
    autocomplete_fields = ["part_type", "part_item", "stock_lot", "to_location"]
    readonly_fields = ("unit_cost_rub", "total_cost_rub", "returned_lot")

    # Lines of a completed/canceled return are posted history: view only.
    def has_add_permission(self, request, obj=None):
        return not _is_posted(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return not _is_posted(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not _is_posted(obj) and super().has_delete_permission(request, obj)


@admin.register(StockReturn)
class StockReturnAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "source_type", "source_id", "cost_total", "completed_at")
    list_filter = ("status", "source_type")
    search_fields = ("number", "reason", "comment")
    # Status changes only through the domain services (complete/cancel), which
    # post or compensate stock.  Editing it here would bypass the ledger.
    readonly_fields = (
        "number", "status", "created_at", "updated_at", "completed_at", "cost_total",
    )
    inlines = [StockReturnLineInline]

    def get_readonly_fields(self, request, obj=None):
        if _is_posted(obj):
            return [field.name for field in obj._meta.concrete_fields]
        return super().get_readonly_fields(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not _is_posted(obj) and super().has_delete_permission(request, obj)
