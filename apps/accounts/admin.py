from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Дополнительно", {"fields": ("full_name",)}),)
    list_display = ("username", "full_name", "email", "is_staff", "is_active")
