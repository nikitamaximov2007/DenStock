from django.apps import AppConfig


class SalesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sales"
    verbose_name = "Продажи (резервы)"

    def ready(self) -> None:
        """Регистрируем провайдер «зарезервировано» в inventory.

        Только регистрация функции — без запросов к БД (ready() выполняется при
        старте, БД может быть ещё не готова). Так inventory считает reserved из
        активных ReservationLine, не импортируя apps.sales напрямую.
        """
        from apps.inventory.services import set_reserved_provider

        from .services import reserved_for

        set_reserved_provider(reserved_for)
