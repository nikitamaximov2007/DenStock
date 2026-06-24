from django.contrib import admin

from .models import (
    Category,
    Manufacturer,
    PartBarcode,
    PartCompatibility,
    PartNumber,
    PartType,
    Unit,
    VehicleMake,
    VehicleModel,
    VehicleType,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "parent", "sort_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Manufacturer)
class ManufacturerAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "is_active")
    search_fields = ("name", "short_name")


@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order", "is_active")
    search_fields = ("name",)


@admin.register(VehicleMake)
class VehicleMakeAdmin(admin.ModelAdmin):
    list_display = ("name", "vehicle_type", "is_active")
    list_filter = ("vehicle_type", "is_active")
    search_fields = ("name",)


@admin.register(VehicleModel)
class VehicleModelAdmin(admin.ModelAdmin):
    list_display = ("name", "vehicle_make", "year_from", "year_to", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


class PartNumberInline(admin.TabularInline):
    model = PartNumber
    extra = 0


class PartBarcodeInline(admin.TabularInline):
    model = PartBarcode
    extra = 0


class PartCompatibilityInline(admin.TabularInline):
    model = PartCompatibility
    extra = 0


@admin.register(PartType)
class PartTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "manufacturer", "tracking_mode", "is_active")
    list_filter = ("tracking_mode", "is_active", "category")
    search_fields = ("name",)
    inlines = [PartNumberInline, PartBarcodeInline, PartCompatibilityInline]
