from django.contrib import admin

from .models import StorageLocation, StorageLocationRenameHistory


@admin.register(StorageLocation)
class StorageLocationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "level", "purpose", "storage_allowed", "is_active")
    list_filter = ("level", "purpose", "storage_allowed", "is_active")
    search_fields = ("code", "name", "barcode")

    def get_readonly_fields(self, request, obj=None):
        if obj is not None:
            return ("code",)
        return ()


@admin.register(StorageLocationRenameHistory)
class StorageLocationRenameHistoryAdmin(admin.ModelAdmin):
    list_display = ("location", "old_code", "new_code", "renamed_by", "renamed_at")
    list_select_related = ("location", "renamed_by")
    search_fields = ("location__code", "old_code", "new_code", "renamed_by__username")
    readonly_fields = ("location", "old_code", "new_code", "renamed_by", "renamed_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
