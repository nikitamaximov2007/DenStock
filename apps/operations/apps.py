from django.apps import AppConfig


class OperationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.operations"
    verbose_name = "Эксплуатация"

    def ready(self):
        from . import emergency_environment  # noqa: F401
        from .write_guard import install_all_guards

        install_all_guards()
