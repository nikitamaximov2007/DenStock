from django.apps import AppConfig


class ActionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.actions"
    verbose_name = "Действия со склада"

    def ready(self) -> None:
        # Подключаем сигнал: у сохранённой таможенной карточки всегда есть
        # текущая версия для исторических списаний.
        from . import signals  # noqa: F401
