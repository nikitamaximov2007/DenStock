from django.apps import AppConfig


class CustomerRequestsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.customer_requests"
    verbose_name = "Заявки клиентов"

    def ready(self):
        from . import events  # noqa: F401
