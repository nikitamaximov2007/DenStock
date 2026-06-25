from django.contrib import admin

from .models import Reservation, ReservationLine


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
