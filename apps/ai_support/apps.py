from django.apps import AppConfig


class AiSupportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai_support"
    verbose_name = "ИИ-поддержка"

    def ready(self):
        from . import checks  # noqa: F401
