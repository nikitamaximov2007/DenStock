from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def canonical_url(channel_id: int, message_id: int) -> str:
    return f"https://t.me/c/{channel_id}/{message_id}"


def iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def message_text(message: Any) -> str:
    return str(getattr(message, "message", None) or getattr(message, "text", "") or "")


def media_metadata(message: Any) -> dict[str, Any]:
    document = getattr(message, "document", None)
    attributes = []
    for attribute in getattr(document, "attributes", None) or []:
        item = {"type": attribute.__class__.__name__}
        for name in (
            "file_name",
            "duration",
            "w",
            "h",
            "title",
            "performer",
            "voice",
            "round_message",
        ):
            value = getattr(attribute, name, None)
            if value not in (None, ""):
                item[name] = value
        attributes.append(item)
    mime_type = str(getattr(document, "mime_type", "") or "")
    is_video = bool(getattr(message, "video", None)) or mime_type.startswith("video/")
    is_audio = bool(
        getattr(message, "audio", None) or getattr(message, "voice", None)
    ) or mime_type.startswith("audio/")
    file_name = next((item["file_name"] for item in attributes if item.get("file_name")), "")
    duration = next(
        (item["duration"] for item in attributes if item.get("duration") is not None), None
    )
    width = next((item["w"] for item in attributes if item.get("w") is not None), None)
    height = next((item["h"] for item in attributes if item.get("h") is not None), None)
    if is_video:
        media_type = "video"
    elif is_audio:
        media_type = "audio"
    elif getattr(message, "photo", None):
        media_type = "image"
    elif document:
        media_type = "document"
    else:
        media_type = "none"
    return {
        "has_media": media_type != "none",
        "media_type": media_type,
        "file_name": file_name or None,
        "mime_type": mime_type or None,
        "size_bytes": getattr(document, "size", None) if document else None,
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "document_attributes": attributes,
    }


def safe_entities(message: Any) -> list[dict[str, Any]]:
    entities = []
    for entity in getattr(message, "entities", None) or []:
        item = {
            "type": entity.__class__.__name__,
            "offset": getattr(entity, "offset", None),
            "length": getattr(entity, "length", None),
        }
        url = getattr(entity, "url", None)
        if url:
            item["url"] = str(url)
        entities.append(item)
    return entities


def content_hash(record: dict[str, Any]) -> str:
    stable = {
        key: record.get(key)
        for key in (
            "message_id",
            "text",
            "edit_date",
            "grouped_id",
            "media_type",
            "file_name",
            "mime_type",
        )
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def message_record(
    message: Any,
    *,
    channel_id: int,
    discovery_source: str,
    navigation_path: list[int] | None = None,
) -> dict[str, Any]:
    message_id = int(message.id)
    record = {
        "channel_id": channel_id,
        "message_id": message_id,
        "canonical_url": canonical_url(channel_id, message_id),
        "date": iso_datetime(getattr(message, "date", None)),
        "edit_date": iso_datetime(getattr(message, "edit_date", None)),
        "text": message_text(message),
        "entities": safe_entities(message),
        "grouped_id": getattr(message, "grouped_id", None),
        "reply_to_message_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
        "discovery_source": discovery_source,
        "navigation_path": navigation_path or [],
        "collection_timestamp": datetime.now(UTC).isoformat(),
        "processing_status": "discovered",
        "errors": [],
    }
    record.update(media_metadata(message))
    record["content_hash"] = content_hash(record)
    return record


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
