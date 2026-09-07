from django.apps import AppConfig


class CustomsOrdersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.customs_orders"
    verbose_name = "Таможенные заказы"
