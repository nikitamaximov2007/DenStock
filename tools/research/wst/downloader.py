from __future__ import annotations

import asyncio
import os
import re
import secrets
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
    timeout_seconds: int = 300,
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
    for attempt in range(retries + 1):
        temporary = target.with_name(f"{target.name}.{secrets.token_hex(4)}.part")
        try:
            downloaded = await asyncio.wait_for(
                client.download_media(message, file=str(temporary)), timeout=timeout_seconds
            )
            if not downloaded or not temporary.exists():
                raise RuntimeError("Telegram returned no media file.")
            actual_size = temporary.stat().st_size
            expected_size = media.get("size_bytes")
            if actual_size <= 0:
                raise RuntimeError("Telegram download produced an empty file.")
            if expected_size not in (None, 0, actual_size):
                raise RuntimeError(
                    f"Telegram download size mismatch: expected {expected_size}, got {actual_size}."
                )
            os.replace(temporary, target)
            return {
                "status": "downloaded",
                "path": str(target),
                "sha256": sha256_file(target),
                "size_bytes": actual_size,
                "attempts": attempt + 1,
            }
        except TimeoutError:
            _remove_temporary(temporary)
            return {
                "status": "retry_pending",
                "error": f"Telegram download timed out after {timeout_seconds}s.",
                "attempts": attempt + 1,
            }
        except Exception as exc:  # noqa: BLE001 - Telethon errors are optional dependencies.
            _remove_temporary(temporary)
            seconds = getattr(exc, "seconds", None)
            if seconds is not None:
                return {
                    "status": "retry_pending",
                    "error": f"FloodWait: retry after {seconds}s",
                    "attempts": attempt + 1,
                }
            if attempt >= retries:
                return {
                    "status": "retry_pending",
                    "error": f"{exc.__class__.__name__}: {exc}",
                    "attempts": attempt + 1,
                }
            await asyncio.sleep(min(2**attempt, 4))
    raise AssertionError("unreachable")


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        # A cancelled downloader may still own an old .part; later retries use a new name.
        return
