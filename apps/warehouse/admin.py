from django.contrib import admin

from .models import StorageLocation


@admin.register(StorageLocation)
class StorageLocationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "level", "purpose", "storage_allowed", "is_active")
    list_filter = ("level", "purpose", "storage_allowed", "is_active")
    search_fields = ("code", "name", "barcode")
