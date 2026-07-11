from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from .models import sha256_file

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def media_directory(media: dict[str, Any], paths: dict[str, Path]) -> Path:
    mime = str(media.get("mime_type") or "")
    name = str(media.get("file_name") or "")
    suffix = Path(name).suffix.lower()
    if media.get("media_type") == "video":
        return paths["videos"]
    if media.get("media_type") == "audio":
        return paths["audio"]
    if media.get("media_type") == "image":
        return paths["images"]
    if suffix in {".zip", ".rar", ".7z", ".tar", ".gz"}:
        return paths["archives"]
    if mime or suffix in {".pdf", ".pptx", ".docx", ".xlsx", ".html", ".htm", ".txt", ".md"}:
        return paths["documents"]
    return paths["unknown"]


def safe_media_name(message_id: int, file_name: str | None, mime_type: str | None = None) -> str:
    original = Path(file_name or "attachment").name
    stem = SAFE_NAME_RE.sub("-", Path(original).stem).strip(".-") or "attachment"
    suffix = Path(original).suffix.lower()
    if not suffix:
        suffix = {
            "video/mp4": ".mp4",
            "audio/mpeg": ".mp3",
            "application/pdf": ".pdf",
            "image/jpeg": ".jpg",
        }.get(str(mime_type or "").lower(), "")
    return f"post-{message_id}-{stem[:80]}{suffix[:12]}"


async def download_media(
    client: Any,
    message: Any,
    media: dict[str, Any],
    paths: dict[str, Path],
    *,
    existing_sha256: str | None = None,
    retries: int = 2,
) -> dict[str, Any]:
    """Download to .part and atomically rename; never keeps a partial final file."""
    message_id = int(message.id)
    target = media_directory(media, paths) / safe_media_name(
        message_id, media.get("file_name"), media.get("mime_type")
    )
    if target.exists():
        digest = sha256_file(target)
        if existing_sha256 and digest == existing_sha256:
            return {
                "status": "skipped",
                "path": str(target),
                "sha256": digest,
                "size_bytes": target.stat().st_size,
            }
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.unlink(missing_ok=True)
    for attempt in range(retries + 1):
        try:
            downloaded = await client.download_media(message, file=str(temporary))
            if not downloaded or not temporary.exists():
                raise RuntimeError("Telegram returned no media file.")
            os.replace(temporary, target)
            return {
                "status": "downloaded",
                "path": str(target),
                "sha256": sha256_file(target),
                "size_bytes": target.stat().st_size,
            }
        except Exception as exc:  # noqa: BLE001 - Telethon errors are optional dependencies.
            temporary.unlink(missing_ok=True)
            seconds = getattr(exc, "seconds", None)
            if seconds is not None:
                return {"status": "flood_wait", "error": f"FloodWait: retry after {seconds}s"}
            if attempt >= retries:
                return {"status": "failed", "error": f"{exc.__class__.__name__}: {exc}"}
            await asyncio.sleep(min(2**attempt, 4))
    raise AssertionError("unreachable")
