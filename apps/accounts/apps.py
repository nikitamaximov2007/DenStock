from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Пользователи"

    def ready(self) -> None:
        # Подключаем сигнал: суперпользователь автоматически попадает в группу
        # «Администратор».
        from . import signals  # noqa: F401
