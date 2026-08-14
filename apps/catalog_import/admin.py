from django.contrib import admin

from .models import CatalogImportBatch


@admin.register(CatalogImportBatch)
class CatalogImportBatchAdmin(admin.ModelAdmin):
    list_display = ("catalog", "source_filename", "status", "created_at", "applied_at")
    list_filter = ("catalog", "status")
    search_fields = ("source_filename", "source_sha256")
    readonly_fields = ("source_sha256", "summary", "apply_summary", "catalog_fingerprint")
