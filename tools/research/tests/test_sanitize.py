from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.research.sanitize import (
    ALLOWED_FIELDS,
    ensure_research_path,
    format_markdown,
    sanitize_record,
    sanitize_text,
    write_jsonl,
)


def test_sanitizer_removes_author_fields() -> None:
    record = {
        "video_id": "abc123",
        "video_url": "https://www.youtube.com/watch?v=abc123",
        "date": "2026-01-01T00:00:00Z",
        "text": "Where can I buy this part?",
        "authorDisplayName": "Private Name",
        "authorChannelId": {"value": "UC-private"},
        "authorProfileImageUrl": "https://example.test/avatar.jpg",
        "authorChannelUrl": "https://youtube.com/channel/UC-private",
    }

    cleaned = sanitize_record(record, "youtube_comments")
    dumped = json.dumps(cleaned, ensure_ascii=False)

    assert "authorDisplayName" not in cleaned
    assert "authorChannelId" not in cleaned
    assert "authorProfileImageUrl" not in cleaned
    assert "Private Name" not in dumped
    assert "UC-private" not in dumped


def test_sanitizer_redacts_contacts_inside_text() -> None:
    text = (
        "Call +7 (999) 123-45-67, email user@example.com, "
        "write @private_user or open https://t.me/private_user"
    )

    redacted = sanitize_text(text)

    assert "[phone redacted]" in redacted
    assert "[email redacted]" in redacted
    assert "[username redacted]" in redacted
    assert "[link redacted]" in redacted
    assert "999" not in redacted
    assert "user@example.com" not in redacted
    assert "@private_user" not in redacted
    assert "https://t.me/private_user" not in redacted


def test_jsonl_writer_writes_only_allowed_fields(tmp_path: Path) -> None:
    target = tmp_path / "research_inputs" / "denis_channels" / "raw" / "youtube_comments.jsonl"
    records = [
        {
            "video_id": "v1",
            "video_url": "https://www.youtube.com/watch?v=v1",
            "date": "2026-01-01T00:00:00Z",
            "text": "Need price, contact me at buyer@example.com",
            "like_count": 3,
            "authorDisplayName": "Private Name",
            "unexpected": "drop me",
        }
    ]

    count = write_jsonl(records, target, "youtube_comments", tmp_path)
    line = target.read_text(encoding="utf-8").strip()
    payload = json.loads(line)

    assert count == 1
    assert set(payload).issubset(set(ALLOWED_FIELDS["youtube_comments"]))
    assert "authorDisplayName" not in payload
    assert "unexpected" not in payload
    assert payload["text"] == "Need price, contact me at [email redacted]"


def test_markdown_formatter_aggregates_comments() -> None:
    markdown = format_markdown(
        [
            {"video_id": "v1", "video_url": "https://youtu.be/v1", "text": "How much?"},
            {"video_id": "v1", "video_url": "https://youtu.be/v1", "text": "How much?"},
        ],
        "youtube_comments",
        "YouTube comments",
    )

    assert "# YouTube comments" in markdown
    assert "Records: 2" in markdown
    assert "Unique sanitized texts: 1" in markdown
    assert "- Count: 2" in markdown
    assert "How much?" in markdown


def test_path_safety_allows_only_research_inputs(tmp_path: Path) -> None:
    allowed = tmp_path / "research_inputs" / "denis_channels" / "raw" / "ok.jsonl"
    resolved = ensure_research_path(allowed, tmp_path)

    assert resolved == allowed.resolve()

    with pytest.raises(ValueError):
        ensure_research_path(tmp_path / "research_inputs" / "outside.jsonl", tmp_path)

    with pytest.raises(ValueError):
        ensure_research_path(tmp_path / "other" / "file.jsonl", tmp_path)
