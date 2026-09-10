"""Version hooks for the draft public-request privacy mechanics.

The identifiers below are configuration, not legal copy or a legal opinion.
Before public launch their text, retention rules, and publication process need
Russian legal review. Requests retain the accepted identifiers permanently.
"""
from django.conf import settings

PUBLIC_REQUEST_CONSENT_PURPOSE = "public_request_contact"


def current_consent_versions() -> tuple[str, str]:
    return (
        settings.PUBLIC_REQUEST_PRIVACY_POLICY_VERSION,
        settings.PUBLIC_REQUEST_PERSONAL_DATA_CONSENT_VERSION,
    )
