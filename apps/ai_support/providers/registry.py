from django.conf import settings

from .disabled import DisabledProvider


def get_provider():
    if not settings.AI_SUPPORT_ENABLED:
        return DisabledProvider("feature_disabled")
    provider = settings.AI_SUPPORT_PROVIDER.strip().lower()
    if provider == "disabled":
        return DisabledProvider("provider_disabled")
    if provider == "fake":
        if not getattr(settings, "AI_SUPPORT_ALLOW_FAKE_PROVIDER", False):
            return DisabledProvider("provider_not_configured")
        from .fake import FakeProvider

        return FakeProvider()
    if provider == "codex_cli":
        if (
            not settings.AI_SUPPORT_CODEX_MODEL
            or not str(settings.AI_SUPPORT_CODEX_HOME)
            or not str(settings.AI_SUPPORT_CODEX_WORKSPACE)
        ):
            return DisabledProvider("provider_not_configured")
        from .codex_cli import CodexCliProvider

        return CodexCliProvider(
            binary=settings.AI_SUPPORT_CODEX_BINARY,
            model=settings.AI_SUPPORT_CODEX_MODEL,
            codex_home=settings.AI_SUPPORT_CODEX_HOME,
            workspace=settings.AI_SUPPORT_CODEX_WORKSPACE,
            timeout_seconds=settings.AI_SUPPORT_CODEX_TIMEOUT_SECONDS,
            max_output_bytes=settings.AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES,
            max_stderr_bytes=settings.AI_SUPPORT_CODEX_MAX_STDERR_BYTES,
            max_prompt_chars=settings.AI_SUPPORT_CODEX_MAX_PROMPT_CHARS,
            max_history_chars=settings.AI_SUPPORT_CODEX_MAX_HISTORY_CHARS,
            max_concurrent=settings.AI_SUPPORT_CODEX_MAX_CONCURRENT,
        )
    return DisabledProvider("provider_not_configured")
