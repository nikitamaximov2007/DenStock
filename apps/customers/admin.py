from django.contrib import admin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "archived_at", "created_at")
    list_filter = ("archived_at",)
    search_fields = ("name", "phone", "phone_normalized")
    readonly_fields = ("phone_normalized", "archived_at", "archived_by", "created_at", "updated_at")

    def has_delete_permission(self, request, obj=None):
        return False
