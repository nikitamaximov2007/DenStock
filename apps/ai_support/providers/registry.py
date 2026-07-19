from django.conf import settings

from apps.ai_support.contracts import normalize_provider_name

from .disabled import DisabledProvider


def codex_configuration_ready() -> bool:
    launch_mode = settings.AI_SUPPORT_CODEX_LAUNCH_MODE.strip().lower()
    common = bool(
        settings.AI_SUPPORT_CODEX_REQUIRED_VERSION
        and settings.AI_SUPPORT_CODEX_MODEL
        and str(settings.AI_SUPPORT_CODEX_WORKSPACE)
        and settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY == 1
    )
    if launch_mode == "external":
        return common and bool(settings.AI_SUPPORT_CODEX_LAUNCHER_SOCKET)
    if launch_mode == "direct_dev":
        return bool(
            common
            and settings.DEBUG
            and settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION
            and settings.AI_SUPPORT_CODEX_BINARY
            and str(settings.AI_SUPPORT_CODEX_HOME)
        )
    return False


def get_provider():
    if not settings.AI_SUPPORT_ENABLED:
        return DisabledProvider("feature_disabled")
    provider = normalize_provider_name(settings.AI_SUPPORT_PROVIDER)
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
        options = {
            "required_version": settings.AI_SUPPORT_CODEX_REQUIRED_VERSION,
            "model": settings.AI_SUPPORT_CODEX_MODEL,
            "workspace": settings.AI_SUPPORT_CODEX_WORKSPACE,
            "timeout_seconds": settings.AI_SUPPORT_CODEX_TIMEOUT_SECONDS,
            "max_output_bytes": settings.AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES,
            "max_stderr_bytes": settings.AI_SUPPORT_CODEX_MAX_STDERR_BYTES,
            "max_prompt_chars": settings.AI_SUPPORT_CODEX_MAX_PROMPT_CHARS,
            "max_history_chars": settings.AI_SUPPORT_CODEX_MAX_HISTORY_CHARS,
            "global_concurrency": settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY,
        }
        if settings.AI_SUPPORT_CODEX_LAUNCH_MODE.strip().lower() == "external":
            from .external_launcher import ExternalCodexProvider

            return ExternalCodexProvider(
                socket_path=settings.AI_SUPPORT_CODEX_LAUNCHER_SOCKET,
                **options,
            )
        from .codex_cli import CodexCliProvider

        return CodexCliProvider(
            binary=settings.AI_SUPPORT_CODEX_BINARY,
            codex_home=settings.AI_SUPPORT_CODEX_HOME,
            **options,
        )
    return DisabledProvider("provider_not_configured")
