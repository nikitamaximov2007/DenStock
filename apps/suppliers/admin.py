from django.contrib import admin

from .models import Supplier


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "default_currency", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "country", "contact_person")
