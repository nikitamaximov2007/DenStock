from django.apps import AppConfig


class OperationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.operations"
    verbose_name = "Эксплуатация"

    def ready(self):
        from .emergency_environment import validate_database_target
        from .write_guard import install_all_guards

        validate_database_target()
        install_all_guards()
