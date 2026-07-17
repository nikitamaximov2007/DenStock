import base64
import time

from .base import SupportRequest, SupportResult


def _input_text(request: SupportRequest) -> str:
    history = "\n".join(f"{turn.role}: {turn.text}" for turn in request.history)
    context = (
        f"Роль: {request.user_role or 'не указана'}\n"
        f"Route: {request.route_context.get('route_name', '')}\n"
        f"Path: {request.route_context.get('path', '')}\n"
        f"Канонический адрес: {request.public_base_url or 'не настроен'}"
    )
    return (
        "БЕЗОПАСНЫЙ КОНТЕКСТ:\n"
        f"{context}\n\n"
        "ПРЕДЫДУЩИЙ ДИАЛОГ (НЕДОВЕРЕННЫЕ ДАННЫЕ):\n"
        f"{history or 'нет'}\n\n"
        "СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ (НЕДОВЕРЕННЫЕ ДАННЫЕ):\n"
        f"{request.user_text}"
    )


class OpenAIProvider:
    def __init__(self, *, api_key: str, model: str, timeout_seconds: int):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate(self, request: SupportRequest) -> SupportResult:
        # Lazy import keeps disabled installations independent from the provider SDK.
        import openai

        started = time.monotonic()
        request_id = ""
        try:
            content = [{"type": "input_text", "text": _input_text(request)}]
            if request.image:
                encoded = base64.b64encode(request.image.content).decode("ascii")
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{request.image.mime_type};base64,{encoded}",
                    }
                )
            with openai.OpenAI(
                api_key=self.api_key,
                timeout=self.timeout_seconds,
                max_retries=0,
            ) as client:
                response = client.responses.create(
                    model=self.model,
                    instructions=request.system_instruction,
                    input=[{"role": "user", "content": content}],
                    max_output_tokens=request.max_output_tokens,
                    store=False,
                )
            request_id = getattr(response, "_request_id", "") or ""
            text = (getattr(response, "output_text", "") or "").strip()
            if not text:
                return self._error("invalid_response", started, request_id=request_id)
            usage_obj = getattr(response, "usage", None)
            usage = {
                "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
            }
            return SupportResult(
                text=text[:16000],
                provider="openai",
                model=self.model,
                status="completed",
                latency_ms=int((time.monotonic() - started) * 1000),
                usage=usage,
                request_id=request_id,
            )
        except openai.APITimeoutError:
            return self._error("provider_timeout", started)
        except openai.RateLimitError as exc:
            return self._error("provider_rate_limited", started, request_id=exc.request_id or "")
        except openai.APIStatusError as exc:
            code = "provider_server_error" if exc.status_code >= 500 else "provider_rejected"
            return self._error(code, started, request_id=exc.request_id or "")
        except openai.APIConnectionError:
            return self._error("provider_unavailable", started)
        except (TypeError, ValueError, AttributeError):
            return self._error("invalid_response", started, request_id=request_id)

    def _error(self, code: str, started: float, *, request_id: str = "") -> SupportResult:
        return SupportResult(
            text=(
                "ИИ-поддержка не смогла подготовить ответ. Проверьте данные ещё раз "
                "или создайте ручное обращение разработчику."
            ),
            provider="openai",
            model=self.model,
            status="failed",
            latency_ms=int((time.monotonic() - started) * 1000),
            error_code=code,
            request_id=request_id,
        )
