from django.contrib import admin

from .models import Category, Manufacturer, Unit, VehicleMake, VehicleModel, VehicleType


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
