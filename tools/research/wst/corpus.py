from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        records, key=lambda item: str(item.get("chunk_id") or item.get("message_id") or "")
    )
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in ordered),
        encoding="utf-8",
    )


def chunk_id(material_id: str, source_ref: str, text: str) -> str:
    return hashlib.sha256(f"{material_id}|{source_ref}|{text}".encode()).hexdigest()[:20]


def post_chunks(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks = []
    for post in posts:
        text = str(post.get("text") or "").strip()
        if not text:
            continue
        source_ref = f"wst://post/{post['message_id']}"
        material_id = f"post-{post['message_id']}"
        chunks.append(
            {
                "chunk_id": chunk_id(material_id, source_ref, text),
                "material_id": material_id,
                "message_id": post["message_id"],
                "canonical_url": post["canonical_url"],
                "content_type": "post",
                "source_kind": "post_text",
                "text": text,
                "date": post.get("date"),
                "navigation_path": post.get("navigation_path", []),
                "discovery_source": post.get("discovery_source"),
                "confidence": 1.0,
                "source_ref": source_ref,
                "content_hash": post.get("content_hash"),
            }
        )
    return chunks


def transcript_chunks(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    post_id = transcript["post_id"]
    material_id = f"video-{post_id}"
    chunks = []
    for segment in transcript.get("segments", []):
        text = str(segment.get("text") or "").strip()
        source_ref = segment.get("source_ref")
        if not text or not source_ref or "start" not in segment or "end" not in segment:
            continue
        chunks.append(
            {
                "chunk_id": chunk_id(material_id, source_ref, text),
                "material_id": material_id,
                "message_id": post_id,
                "content_type": "video",
                "source_kind": "transcript",
                "text": text,
                "timestamp_start": segment["start"],
                "timestamp_end": segment["end"],
                "confidence": segment.get("confidence"),
                "source_ref": source_ref,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    return chunks


def document_chunks(document: dict[str, Any]) -> list[dict[str, Any]]:
    material_id = f"document-{document['post_id']}-{document['file_name']}"
    chunks = []
    for block in document.get("blocks", []):
        text, source_ref = str(block.get("text") or "").strip(), block.get("source_ref")
        if not text or not source_ref:
            continue
        chunks.append(
            {
                "chunk_id": chunk_id(material_id, source_ref, text),
                "material_id": material_id,
                "message_id": document["post_id"],
                "content_type": "document",
                "source_kind": Path(document["file_name"]).suffix.lower().lstrip(".") or "document",
                "text": text,
                "page": block.get("page"),
                "slide": block.get("slide"),
                "sheet": block.get("sheet"),
                "confidence": 1.0,
                "source_ref": source_ref,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    return chunks


def ocr_chunks(ocr: dict[str, Any]) -> list[dict[str, Any]]:
    post_id = ocr["post_id"]
    material_id = f"video-{post_id}"
    chunks = []
    for frame in ocr.get("frames", []):
        text = str(frame.get("normalized_text") or "").strip()
        source_ref = frame.get("source_ref")
        if not text or not source_ref or not frame.get("timestamp"):
            continue
        chunks.append(
            {
                "chunk_id": chunk_id(material_id, source_ref, text),
                "material_id": material_id,
                "message_id": post_id,
                "content_type": "video",
                "source_kind": "frame_ocr",
                "text": text,
                "timestamp_start": frame["timestamp"],
                "confidence": frame.get("confidence"),
                "source_ref": source_ref,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    return chunks


def build_fts(index_path: Path, chunks: list[dict[str, Any]]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(index_path) as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
            "chunk_id UNINDEXED, text, source_ref UNINDEXED, "
            "source_kind UNINDEXED, message_id UNINDEXED)"
        )
        for item in chunks:
            connection.execute("DELETE FROM chunks WHERE chunk_id = ?", (item["chunk_id"],))
            connection.execute(
                "INSERT INTO chunks(chunk_id, text, source_ref, source_kind, message_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    item["chunk_id"],
                    item["text"],
                    item["source_ref"],
                    item["source_kind"],
                    str(item["message_id"]),
                ),
            )


def search_fts(index_path: Path, query: str, limit: int = 20) -> list[dict[str, str]]:
    with sqlite3.connect(index_path) as connection:
        rows = connection.execute(
            "SELECT chunk_id, snippet(chunks, 1, '[', ']', '…', 18), source_ref, "
            "source_kind, message_id FROM chunks WHERE chunks MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
    return [
        dict(zip(("chunk_id", "text", "source_ref", "source_kind", "message_id"), row, strict=True))
        for row in rows
    ]


def validate_chunks(chunks: list[dict[str, Any]], media_root: Path | None = None) -> list[str]:
    errors = []
    seen = set()
    for item in chunks:
        identifier = item.get("chunk_id")
        if not identifier or identifier in seen:
            errors.append(f"Duplicate or missing chunk_id: {identifier}")
        seen.add(identifier)
        if not item.get("source_ref"):
            errors.append(f"Chunk {identifier} has no source_ref")
        if item.get("source_kind") == "transcript" and not (
            item.get("timestamp_start") and item.get("timestamp_end")
        ):
            errors.append(f"Transcript chunk {identifier} has no timestamps")
        if item.get("source_kind") == "frame_ocr" and not item.get("timestamp_start"):
            errors.append(f"OCR chunk {identifier} has no frame timestamp")
        if item.get("source_kind") in {"pdf", "pptx", "docx", "xlsx", "html"} and not any(
            item.get(key) for key in ("page", "slide", "sheet")
        ):
            errors.append(f"Document chunk {identifier} has no structural location")
    if media_root is not None and not media_root.exists():
        errors.append("Media root is missing")
    return errors


def build_packs(chunks: list[dict[str, Any]], packs_dir: Path, max_bytes: int) -> list[str]:
    packs_dir.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in chunks:
        groups.setdefault(item["material_id"], []).append(item)
    packs, current, current_size = [], [], 0
    for material in groups.values():
        block = "\n\n".join(f"{item['source_ref']}\n\n{item['text']}" for item in material) + "\n"
        size = len(block.encode("utf-8"))
        if current and current_size + size > max_bytes:
            packs.append("\n\n".join(current))
            current, current_size = [], 0
        current.append(block)
        current_size += size
    if current:
        packs.append("\n\n".join(current))
    names = []
    for number, content in enumerate(packs, start=1):
        name = f"wst-pack-{number:04}.md"
        (packs_dir / name).write_text(content, encoding="utf-8")
        names.append(name)
    return names
