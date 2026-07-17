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


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


@register(Tags.security)
def codex_runtime_is_isolated(app_configs, **kwargs):
    if not settings.AI_SUPPORT_ENABLED or settings.AI_SUPPORT_PROVIDER != "codex_cli":
        return []
    errors = []
    raw_home = str(settings.AI_SUPPORT_CODEX_HOME).strip()
    raw_workspace = str(settings.AI_SUPPORT_CODEX_WORKSPACE).strip()
    if not settings.AI_SUPPORT_CODEX_MODEL:
        errors.append(Error("AI_SUPPORT_CODEX_MODEL is required.", id="ai_support.E002"))
    if not raw_home or not Path(raw_home).is_absolute():
        errors.append(
            Error("AI_SUPPORT_CODEX_HOME must be an absolute path.", id="ai_support.E003")
        )
    if not raw_workspace or not Path(raw_workspace).is_absolute():
        errors.append(
            Error(
                "AI_SUPPORT_CODEX_WORKSPACE must be an absolute path.",
                id="ai_support.E004",
            )
        )
    if errors:
        return errors

    home = Path(raw_home).resolve()
    workspace = Path(raw_workspace).resolve()
    if not home.is_dir() or not workspace.is_dir():
        errors.append(
            Error(
                "Codex home and workspace must already exist as directories.",
                id="ai_support.E005",
            )
        )
    protected = {
        Path(settings.BASE_DIR).resolve(),
        Path(settings.MEDIA_ROOT).resolve(),
        Path(settings.PRIVATE_MEDIA_ROOT).resolve(),
        Path(settings.BACKUP_ROOT).resolve(),
    }
    if _overlaps(home, workspace) or any(
        _overlaps(path, protected_path)
        for path in (home, workspace)
        for protected_path in protected
    ):
        errors.append(
            Error(
                "Codex home and workspace must be isolated from DenisStock data.",
                id="ai_support.E006",
            )
        )
    limits = (
        settings.AI_SUPPORT_CODEX_TIMEOUT_SECONDS,
        settings.AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES,
        settings.AI_SUPPORT_CODEX_MAX_STDERR_BYTES,
        settings.AI_SUPPORT_CODEX_MAX_PROMPT_CHARS,
        settings.AI_SUPPORT_CODEX_MAX_CONCURRENT,
    )
    if any(value <= 0 for value in limits):
        errors.append(Error("Codex runtime limits must be positive.", id="ai_support.E007"))
    return errors
