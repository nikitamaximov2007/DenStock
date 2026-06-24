from django.contrib import admin

from .models import Batch, BatchLine


class BatchLineInline(admin.TabularInline):
    model = BatchLine
    extra = 0
    readonly_fields = (
        "unit_cost_rub",
        "total_cost_currency",
        "total_cost_rub",
        "allocated_overhead_rub",
        "landed_unit_cost_rub",
        "landed_total_cost_rub",
    )


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("number", "supplier", "status", "currency", "cost_finalized", "created_at")
    list_filter = ("status", "cost_finalized", "supplier")
    search_fields = ("number", "order_number", "invoice_number")
    inlines = [BatchLineInline]
    readonly_fields = (
        "number",
        "total_extra_cost",
        "cost_finalized",
        "cost_finalized_at",
        "cost_finalized_by",
    )
