from .base import SupportRequest, SupportResult


class FakeProvider:
    """Deterministic provider used only when tests explicitly enable it."""

    def generate(self, request: SupportRequest) -> SupportResult:
        body = "\n\n".join(request.knowledge_chunks).lower()
        if "ssl_protocol_error" in request.user_text.lower() or "голому ip" in body:
            base_url = request.public_base_url or "канонический адрес не настроен"
            text = (
                "1. Что, вероятно, произошло.\n"
                "Открыт HTTPS по голому IP, поэтому браузер не смог проверить сертификат.\n\n"
                "2. Что проверить сейчас.\n"
                "Проверьте список продаж: появилась ли продажа, проведена ли она, "
                "изменились ли остатки и нет ли дубля.\n\n"
                "3. Что сделать.\n"
                f"Откройте DenisStock по каноническому адресу: {base_url}. Не нажимайте "
                "«Провести продажу» повторно до проверки списка.\n\n"
                "4. Как убедиться, что проблема решена.\n"
                "Убедитесь, что сайт открывается, а документ и остатки отображаются один раз.\n\n"
                "5. Когда передать проблему разработчику.\n"
                "Создайте обращение, если канонический адрес перенаправляет на голый IP."
            )
        else:
            text = (
                "1. Что, вероятно, произошло.\nНужно уточнить детали проблемы.\n\n"
                "2. Что проверить сейчас.\nПроверьте текущий раздел и статус документа.\n\n"
                "3. Что сделать.\nОпишите последний безопасный шаг и текст ошибки.\n\n"
                "4. Как убедиться, что проблема решена.\nПроверьте результат один раз.\n\n"
                "5. Когда передать проблему разработчику.\n"
                "Если ошибка повторяется, создайте обращение."
            )
        return SupportResult(
            text=text,
            provider="fake",
            model="fake-support-model",
            status="completed",
            latency_ms=1,
            usage={"input_tokens": 20, "output_tokens": 80},
            request_id="fake-request",
        )
