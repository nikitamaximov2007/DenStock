from __future__ import annotations

from decimal import Decimal

from tools.research.wst.video_frames import parse_scene_timestamps, periodic_timestamps
from tools.research.wst.video_transcriber import (
    deduplicate_overlap,
    format_timestamp,
    parse_ffprobe,
    transcription_chunks,
    write_transcript,
)


def test_ffprobe_and_timestamps_use_decimal_precision() -> None:
    metadata = parse_ffprobe(
        {"format": {"duration": "1.001"}, "streams": [{"codec_type": "audio"}]}
    )

    assert metadata["duration_seconds"] == Decimal("1.001")
    assert format_timestamp(Decimal("3661.001")) == "01:01:01.001"


def test_chunking_keeps_absolute_time_and_deduplicates_overlap() -> None:
    chunks = transcription_chunks(Decimal("3605"), chunk_seconds=1800, overlap_seconds=5)
    segments = deduplicate_overlap(
        [
            {"start": "1795", "end": "1800", "text": "same"},
            {"start": "1795.000", "end": "1800", "text": "same"},
            {"start": "1800", "end": "1810", "text": "next"},
        ]
    )

    assert chunks == [
        (Decimal("0"), Decimal("1800")),
        (Decimal("1795"), Decimal("3595")),
        (Decimal("3590"), Decimal("3605")),
    ]
    assert len(segments) == 2


def test_transcript_marks_empty_segment_without_inventing_text(tmp_path) -> None:
    json_path, markdown_path = write_transcript(
        7, Decimal("2"), [{"start": "0", "end": "1", "text": ""}], tmp_path
    )

    assert json_path.exists()
    assert "[НЕРАЗБОРЧИВО]" in markdown_path.read_text(encoding="utf-8")


def test_scene_and_periodic_timestamps() -> None:
    assert parse_scene_timestamps(["0.000,scene", "bad", "1.500,scene"]) == [
        Decimal("0.000"),
        Decimal("1.500"),
    ]
    assert periodic_timestamps(Decimal("601"), 300) == [
        Decimal("0"),
        Decimal("300"),
        Decimal("600"),
    ]
