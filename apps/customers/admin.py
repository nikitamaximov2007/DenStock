from django.contrib import admin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "created_at")
    search_fields = ("name", "phone", "phone_normalized")
    readonly_fields = ("phone_normalized", "created_at", "updated_at")
