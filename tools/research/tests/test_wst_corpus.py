from __future__ import annotations

from pathlib import Path

from tools.research.wst.corpus import (
    build_fts,
    build_packs,
    post_chunks,
    search_fts,
    transcript_chunks,
    validate_chunks,
)


def test_chunks_keep_source_refs_and_fts_returns_them(tmp_path: Path) -> None:
    chunks = post_chunks(
        [
            {
                "message_id": 3,
                "canonical_url": "https://t.me/c/3278525266/3",
                "text": "целевая аудитория",
                "content_hash": "hash",
            }
        ]
    )
    index = tmp_path / "index.sqlite3"
    build_fts(index, chunks)

    results = search_fts(index, "целевая")

    assert chunks[0]["source_ref"] == "wst://post/3"
    assert results[0]["source_ref"] == "wst://post/3"


def test_transcript_requires_timestamps_and_validation_finds_missing_ref() -> None:
    valid = transcript_chunks(
        {
            "post_id": 5,
            "segments": [
                {
                    "start": "0",
                    "end": "1",
                    "text": "text",
                    "source_ref": "wst://post/5/video?t=00:00:00-00:00:01",
                }
            ],
        }
    )
    broken = {**valid[0], "chunk_id": "broken", "source_ref": ""}

    assert validate_chunks(valid) == []
    assert "no source_ref" in validate_chunks([broken])[0]


def test_packs_keep_material_blocks_together(tmp_path: Path) -> None:
    chunks = [
        {"material_id": "a", "source_ref": "wst://post/1", "text": "one"},
        {"material_id": "a", "source_ref": "wst://post/1", "text": "two"},
        {"material_id": "b", "source_ref": "wst://post/2", "text": "three"},
    ]

    names = build_packs(chunks, tmp_path, 30)

    assert names
    assert "one" in (tmp_path / names[0]).read_text(encoding="utf-8")
    assert "two" in (tmp_path / names[0]).read_text(encoding="utf-8")
