from django.contrib import admin

from .models import PartCustomsInfo, WarehouseAction


@admin.register(WarehouseAction)
class WarehouseActionAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "action_type", "part_type", "location",
        "quantity", "total_price_rub", "customer_comment", "created_by",
    )
    list_filter = ("action_type",)
    search_fields = ("customer_comment", "part_type__name")


@admin.register(PartCustomsInfo)
class PartCustomsInfoAdmin(admin.ModelAdmin):
    list_display = (
        "part_type", "customs_name_ru", "gross_weight_kg",
        "net_weight_kg", "weight_verified",
    )
    search_fields = ("part_type__name", "customs_name_ru")
