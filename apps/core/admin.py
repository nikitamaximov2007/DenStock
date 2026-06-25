from django.contrib import admin

from .models import UnresolvedScan


@admin.register(UnresolvedScan)
class UnresolvedScanAdmin(admin.ModelAdmin):
    """Журнал нераспознанных сканов: первичные поля только на чтение; разбор
    (`resolved`/`resolved_part`/`note`) можно проставить вручную."""

    list_display = (
        "raw_value", "normalized_value", "user", "context", "resolved", "created_at",
    )
    list_filter = ("resolved", "context")
    search_fields = ("raw_value", "normalized_value")
    readonly_fields = ("raw_value", "normalized_value", "user", "context", "created_at")

    def has_add_permission(self, request):
        return False
