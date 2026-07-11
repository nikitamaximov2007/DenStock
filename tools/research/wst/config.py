from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tools.research.collect_denis_sources import load_env_file
from tools.research.sanitize import PROJECT_ROOT
from tools.research.telegram_collector import default_session_path

WST_CHANNEL_ID = 3278525266
WST_NAVIGATION_MESSAGE_ID = 3
DEFAULT_OUTPUT_ROOT = Path("research_inputs") / "wst"


@dataclass(frozen=True)
class WSTSettings:
    api_id: str
    api_hash: str
    channel_id: int
    navigation_message_id: int
    output_root: Path
    whisper_model: str
    ocr_languages: str

    @property
    def has_telegram_credentials(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def session_path(self) -> Path:
        """Reuse the existing Denis research session without exposing it in CLI output."""
        return default_session_path()


def resolve_output_root(value: str | Path | None, project_root: Path = PROJECT_ROOT) -> Path:
    candidate = Path(value) if value else DEFAULT_OUTPUT_ROOT
    if not candidate.is_absolute():
        candidate = project_root / candidate
    resolved = candidate.resolve()
    allowed = (project_root / "research_inputs" / "wst").resolve()
    if resolved != allowed and not resolved.is_relative_to(allowed):
        raise ValueError("WST output must stay under research_inputs/wst.")
    return resolved


def load_settings(
    *,
    environ: dict[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
    output_root: str | Path | None = None,
    channel_id: int | None = None,
    navigation_message_id: int | None = None,
) -> WSTSettings:
    file_values = load_env_file(project_root / ".env.research.local")
    env = {**file_values, **dict(os.environ if environ is None else environ)}
    configured_root = output_root if output_root is not None else env.get("WST_OUTPUT_ROOT")
    return WSTSettings(
        api_id=env.get("TG_API_ID", ""),
        api_hash=env.get("TG_API_HASH", ""),
        channel_id=int(channel_id or env.get("WST_CHANNEL_ID", WST_CHANNEL_ID)),
        navigation_message_id=int(
            navigation_message_id or env.get("WST_NAVIGATION_MESSAGE_ID", WST_NAVIGATION_MESSAGE_ID)
        ),
        output_root=resolve_output_root(configured_root, project_root),
        whisper_model=env.get("WST_WHISPER_MODEL", "large-v3"),
        ocr_languages=env.get("WST_OCR_LANGUAGES", "rus+eng"),
    )


def wst_paths(settings: WSTSettings) -> dict[str, Path]:
    root = settings.output_root
    paths = {
        "root": root,
        "raw": root / "raw",
        "media": root / "media",
        "videos": root / "media" / "videos",
        "audio": root / "media" / "audio",
        "documents": root / "media" / "documents",
        "images": root / "media" / "images",
        "archives": root / "media" / "archives",
        "unknown": root / "media" / "unknown",
        "state": root / "state",
        "extracted": root / "extracted",
        "transcripts": root / "extracted" / "transcripts",
        "keyframes": root / "extracted" / "keyframes",
        "ocr": root / "extracted" / "ocr",
        "extracted_documents": root / "extracted" / "documents",
        "normalized": root / "normalized",
        "index": root / "index",
        "ai_corpus": root / "ai_corpus",
        "reports": root / "reports",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths
