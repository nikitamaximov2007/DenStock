from django.contrib import admin

from .models import StockReturn, StockReturnLine


def _draft_only(obj):
    return obj is None or obj.status == StockReturn.Status.DRAFT


class StockReturnLineInline(admin.TabularInline):
    model = StockReturnLine
    extra = 0
    autocomplete_fields = ["part_type", "part_item", "stock_lot", "to_location"]
    readonly_fields = ("unit_cost_rub", "total_cost_rub", "returned_lot")

    def has_add_permission(self, request, obj=None):
        return _draft_only(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return _draft_only(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return _draft_only(obj) and super().has_delete_permission(request, obj)


@admin.register(StockReturn)
class StockReturnAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "source_type", "source_id", "cost_total", "completed_at")
    list_filter = ("status", "source_type")
    search_fields = ("number", "reason", "comment")
    readonly_fields = ("number", "created_at", "updated_at", "completed_at", "cost_total")
    inlines = [StockReturnLineInline]

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj and obj.status != StockReturn.Status.DRAFT:
            return tuple(fields) + (
                "status", "source_type", "source_id", "created_by", "completed_by", "canceled_at",
                "canceled_by", "cancel_reason",
            )
        return fields

    def has_delete_permission(self, request, obj=None):
        return _draft_only(obj) and super().has_delete_permission(request, obj)
