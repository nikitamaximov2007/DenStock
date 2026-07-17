from .base import SupportRequest, SupportResult


class DisabledProvider:
    def __init__(self, error_code: str = "provider_disabled"):
        self.error_code = error_code

    def generate(self, request: SupportRequest) -> SupportResult:
        return SupportResult(
            text=(
                "ИИ-поддержка сейчас недоступна. Вы можете создать ручное обращение "
                "разработчику и приложить безопасную диагностику."
            ),
            provider="disabled",
            model="",
            status="unavailable",
            error_code=self.error_code,
        )
