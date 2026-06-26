from django.contrib import admin

from .models import WriteOffDocument, WriteOffLine


class WriteOffLineInline(admin.TabularInline):
    model = WriteOffLine
    extra = 0
    autocomplete_fields = ["part_type", "part_item", "stock_lot"]
    readonly_fields = ("unit_cost_rub", "total_cost_rub", "written_off_at")


@admin.register(WriteOffDocument)
class WriteOffDocumentAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "reason", "cost_total", "completed_at")
    list_filter = ("status", "reason")
    search_fields = ("number", "comment")
    readonly_fields = (
        "number", "created_at", "updated_at", "completed_at", "canceled_at", "cost_total",
    )
    inlines = [WriteOffLineInline]
