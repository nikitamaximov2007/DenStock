from django.contrib import admin

from .models import Reservation, ReservationLine, Sale, SaleLine


class ReservationLineInline(admin.TabularInline):
    model = ReservationLine
    extra = 0
    autocomplete_fields = ["part_type", "part_item", "stock_lot"]


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "customer_name", "expires_at", "created_at")
    list_filter = ("status",)
    search_fields = ("number", "customer_name", "customer_phone")
    readonly_fields = ("number", "created_at", "updated_at", "canceled_at")
    inlines = [ReservationLineInline]


class SaleLineInline(admin.TabularInline):
    model = SaleLine
    extra = 0
    autocomplete_fields = ["part_type", "part_item", "stock_lot"]
    readonly_fields = ("unit_cost_rub", "total_cost_rub", "profit_rub")


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "customer_name", "revenue_total", "sold_at")
    list_filter = ("status",)
    search_fields = ("number", "customer_name", "customer_phone")
    readonly_fields = (
        "number", "created_at", "updated_at", "sold_at", "canceled_at",
        "revenue_total", "cost_total", "profit_total",
    )
    inlines = [SaleLineInline]
