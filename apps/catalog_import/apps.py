from django.apps import AppConfig


class CatalogImportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.catalog_import"
    verbose_name = "Импорт каталога"
