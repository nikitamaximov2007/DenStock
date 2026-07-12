from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from .models import sha256_file
from .state import WSTState

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
    state: WSTState | None = None,
    chunk_size: int = 4 * 1024 * 1024,
    checkpoint_bytes: int = 16 * 1024 * 1024,
    resume: bool = True,
    restart: bool = False,
) -> dict[str, Any]:
    """Read-only resumable Telethon stream with durable local progress checkpoints."""
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
    expected_size = _safe_int(media.get("size_bytes"))
    saved = state.download_record(message_id) if state else None
    if restart:
        _remove_temporary(temporary)
        saved = None
    offset, recovery = _resume_offset(temporary, saved, resume)
    if temporary.exists() and temporary.stat().st_size > offset:
        with temporary.open("r+b") as handle:
            handle.truncate(offset)
    if state:
        state.checkpoint_download(
            message_id,
            expected_size=expected_size,
            downloaded_bytes=offset,
            verified_bytes=offset,
            chunk_size=chunk_size,
            current_offset=offset,
            temporary_path=str(temporary),
            attempt_count=int((saved or {}).get("attempt_count", 0)),
            last_successful_offset=offset,
            status="downloading",
            last_error="",
            next_action="Continue resumable Telegram stream.",
        )
    for attempt in range(retries + 1):
        try:
            offset = await _stream_download(
                client,
                message,
                temporary,
                offset,
                chunk_size,
                checkpoint_bytes,
                timeout_seconds,
                state,
                message_id,
                expected_size,
                attempt + 1,
            )
            actual_size = temporary.stat().st_size
            if actual_size <= 0:
                raise RuntimeError("Telegram download produced an empty file.")
            if expected_size not in (None, 0, actual_size):
                raise RuntimeError(
                    f"Telegram download size mismatch: expected {expected_size}, got {actual_size}."
                )
            os.replace(temporary, target)
            if state:
                state.checkpoint_download(
                    message_id,
                    expected_size=expected_size,
                    downloaded_bytes=actual_size,
                    verified_bytes=actual_size,
                    chunk_size=chunk_size,
                    current_offset=actual_size,
                    temporary_path="",
                    attempt_count=attempt + 1,
                    last_successful_offset=actual_size,
                    status="complete",
                    last_error="",
                    next_action="",
                )
            return {
                "status": "downloaded",
                "path": str(target),
                "sha256": sha256_file(target),
                "size_bytes": actual_size,
                "attempts": attempt + 1,
                "resumed_from": recovery["offset"],
                "recovery": recovery["decision"],
            }
        except TimeoutError:
            if state:
                state.checkpoint_download(
                    message_id,
                    status="retry_pending",
                    last_error=f"Timed out after {timeout_seconds}s.",
                    next_action="Resume the existing .part with the same command.",
                )
            return {
                "status": "retry_pending",
                "error": f"Telegram download timed out after {timeout_seconds}s.",
                "attempts": attempt + 1,
                "resumed_from": recovery["offset"],
            }
        except Exception as exc:  # noqa: BLE001 - Telethon errors are optional dependencies.
            seconds = getattr(exc, "seconds", None)
            if seconds is not None:
                if state:
                    state.checkpoint_download(
                        message_id,
                        status="retry_pending",
                        last_error=f"FloodWait: {seconds}s",
                        next_action="Wait for FloodWait, then resume the existing .part.",
                    )
                return {
                    "status": "retry_pending",
                    "error": f"FloodWait: retry after {seconds}s",
                    "attempts": attempt + 1,
                }
            if attempt >= retries:
                if state:
                    state.checkpoint_download(
                        message_id,
                        status="retry_pending",
                        last_error=f"{exc.__class__.__name__}: {exc}",
                        next_action=(
                            "Retry the existing .part; refresh message reference if needed."
                        ),
                    )
                return {
                    "status": "retry_pending",
                    "error": f"{exc.__class__.__name__}: {exc}",
                    "attempts": attempt + 1,
                }
            await asyncio.sleep(min(2**attempt, 4))
    raise AssertionError("unreachable")


async def _stream_download(
    client: Any,
    message: Any,
    temporary: Path,
    offset: int,
    chunk_size: int,
    checkpoint_bytes: int,
    timeout_seconds: int,
    state: WSTState | None,
    message_id: int,
    expected_size: int | None,
    attempt: int,
) -> int:
    checkpoint_at = offset + checkpoint_bytes
    media = getattr(message, "media", message)
    with temporary.open("ab") as handle:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in client.iter_download(
                media,
                offset=offset,
                request_size=chunk_size,
                chunk_size=chunk_size,
                file_size=expected_size,
            ):
                handle.write(chunk)
                offset += len(chunk)
                if offset >= checkpoint_at:
                    handle.flush()
                    os.fsync(handle.fileno())
                    if state:
                        state.checkpoint_download(
                            message_id,
                            expected_size=expected_size,
                            downloaded_bytes=offset,
                            verified_bytes=offset,
                            chunk_size=chunk_size,
                            current_offset=offset,
                            temporary_path=str(temporary),
                            attempt_count=attempt,
                            last_successful_offset=offset,
                            status="downloading",
                            last_error="",
                        )
                    checkpoint_at = offset + checkpoint_bytes
        handle.flush()
        os.fsync(handle.fileno())
    if state:
        state.checkpoint_download(
            message_id,
            expected_size=expected_size,
            downloaded_bytes=offset,
            verified_bytes=offset,
            chunk_size=chunk_size,
            current_offset=offset,
            temporary_path=str(temporary),
            attempt_count=attempt,
            last_successful_offset=offset,
            status="downloaded",
            last_error="",
        )
    return offset


def _resume_offset(
    temporary: Path, saved: dict[str, Any] | None, resume: bool
) -> tuple[int, dict[str, Any]]:
    actual = temporary.stat().st_size if temporary.exists() else 0
    stored = _safe_int((saved or {}).get("last_successful_offset")) or 0
    if not resume:
        return 0, {"offset": 0, "decision": "resume_disabled"}
    offset = min(actual, stored) if stored else actual
    decision = "resume_existing_part" if offset else "start_new"
    if actual != stored and stored:
        decision = "truncate_unverified_tail" if actual > stored else "state_ahead_of_file"
    return offset, {"offset": offset, "decision": decision}


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        # An active process may still own the part file on Windows; preserve it for resume.
        return
