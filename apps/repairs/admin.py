from django.contrib import admin

from .models import RepairIssueLine, RepairOrder


class RepairIssueLineInline(admin.TabularInline):
    model = RepairIssueLine
    extra = 0
    autocomplete_fields = ["part_type", "part_item", "stock_lot"]
    readonly_fields = ("unit_cost_rub", "total_cost_rub", "issued_at")


@admin.register(RepairOrder)
class RepairOrderAdmin(admin.ModelAdmin):
    list_display = (
        "number", "status", "customer_name", "vehicle_type", "cost_total", "completed_at",
    )
    list_filter = ("status", "vehicle_type")
    search_fields = ("number", "customer_name", "customer_phone", "vehicle_identifier")
    readonly_fields = (
        "number", "created_at", "updated_at", "completed_at", "canceled_at", "cost_total",
    )
    inlines = [RepairIssueLineInline]
