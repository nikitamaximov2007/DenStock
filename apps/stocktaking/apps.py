from django.apps import AppConfig


class StocktakingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.stocktaking"
    verbose_name = "Инвентаризация (сверка остатков)"
