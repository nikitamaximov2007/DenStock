from django.apps import AppConfig


class OrderedPartsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ordered_parts"
    verbose_name = "Запчасти на заказ"
