from django.conf import settings

from .disabled import DisabledProvider


def codex_configuration_ready() -> bool:
    launch_mode = settings.AI_SUPPORT_CODEX_LAUNCH_MODE.strip().lower()
    launch_allowed = launch_mode == "external" or (
        launch_mode == "direct_dev"
        and settings.DEBUG
        and settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION
    )
    return bool(
        settings.AI_SUPPORT_CODEX_BINARY
        and settings.AI_SUPPORT_CODEX_REQUIRED_VERSION
        and settings.AI_SUPPORT_CODEX_MODEL
        and str(settings.AI_SUPPORT_CODEX_HOME)
        and str(settings.AI_SUPPORT_CODEX_WORKSPACE)
        and settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY == 1
        and launch_allowed
    )


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
        if not codex_configuration_ready():
            return DisabledProvider("provider_not_configured")
        from .codex_cli import CodexCliProvider

        return CodexCliProvider(
            binary=settings.AI_SUPPORT_CODEX_BINARY,
            required_version=settings.AI_SUPPORT_CODEX_REQUIRED_VERSION,
            model=settings.AI_SUPPORT_CODEX_MODEL,
            codex_home=settings.AI_SUPPORT_CODEX_HOME,
            workspace=settings.AI_SUPPORT_CODEX_WORKSPACE,
            timeout_seconds=settings.AI_SUPPORT_CODEX_TIMEOUT_SECONDS,
            max_output_bytes=settings.AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES,
            max_stderr_bytes=settings.AI_SUPPORT_CODEX_MAX_STDERR_BYTES,
            max_prompt_chars=settings.AI_SUPPORT_CODEX_MAX_PROMPT_CHARS,
            max_history_chars=settings.AI_SUPPORT_CODEX_MAX_HISTORY_CHARS,
            global_concurrency=settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY,
        )
    return DisabledProvider("provider_not_configured")
