from django.contrib import admin

from .models import PartCustomsDataVersion, PartCustomsInfo, WarehouseAction


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


@admin.register(PartCustomsDataVersion)
class PartCustomsDataVersionAdmin(admin.ModelAdmin):
    list_display = (
        "part_type", "version", "country_of_origin", "customs_unit_price_usd", "created_at"
    )
    search_fields = ("part_type__name", "customs_name_ru", "source_reference")
    readonly_fields = (
        "part_type", "version", "effective_from", "customs_name_ru", "customs_name_en",
        "manufacturer", "country_of_origin", "gross_weight_kg", "net_weight_kg",
        "customs_unit_price_usd", "application_area", "source_reference", "created_at",
        "created_by",
    )
