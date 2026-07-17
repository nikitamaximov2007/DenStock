from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security)
def private_media_is_not_public(app_configs, **kwargs):
    private_root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    public_root = Path(settings.MEDIA_ROOT).resolve()
    if private_root == public_root or public_root in private_root.parents:
        return [
            Error(
                "PRIVATE_MEDIA_ROOT must be outside MEDIA_ROOT.",
                hint="Use a private directory mounted only into the Django web service.",
                id="ai_support.E001",
            )
        ]
    return []
