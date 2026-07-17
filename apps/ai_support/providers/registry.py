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
    if provider == "openai":
        if not settings.AI_SUPPORT_API_KEY or not settings.AI_SUPPORT_MODEL:
            return DisabledProvider("provider_not_configured")
        from .openai import OpenAIProvider

        return OpenAIProvider(
            api_key=settings.AI_SUPPORT_API_KEY,
            model=settings.AI_SUPPORT_MODEL,
            timeout_seconds=settings.AI_SUPPORT_TIMEOUT_SECONDS,
        )
    return DisabledProvider("provider_not_configured")
