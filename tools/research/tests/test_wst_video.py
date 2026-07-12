from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tools.research.wst.state import WSTState
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


def test_transcription_resume_skips_completed_local_chunk(tmp_path, monkeypatch) -> None:
    from tools.research.wst import video_transcriber

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    calls: list[Path] = []

    def fake_extract(_audio, _start, _end, directory, number):
        path = directory / f"{number}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"chunk")
        return path

    def fake_transcribe(path, **_kwargs):
        calls.append(path)
        return [{"start": "0", "end": "1", "text": path.stem}]

    monkeypatch.setattr(video_transcriber, "extract_audio_chunk", fake_extract)
    monkeypatch.setattr(video_transcriber, "transcribe_audio", fake_transcribe)
    with WSTState(tmp_path / "state.sqlite3") as state:
        first, first_results = video_transcriber.transcribe_in_chunks(
            audio,
            Decimal("11"),
            tmp_path / "tmp",
            model_name="small",
            device="cpu",
            compute_type="int8",
            chunk_seconds=5,
            overlap_seconds=0,
            state=state,
            message_id=9,
            artifact_dir=tmp_path / "artifacts",
            stop_after_completed=1,
        )
        second, second_results = video_transcriber.transcribe_in_chunks(
            audio,
            Decimal("11"),
            tmp_path / "tmp",
            model_name="small",
            device="cpu",
            compute_type="int8",
            chunk_seconds=5,
            overlap_seconds=0,
            state=state,
            message_id=9,
            artifact_dir=tmp_path / "artifacts",
        )

    assert len(first) == 1
    assert first_results[1]["status"] == "pending"
    assert second_results[0]["skipped_completed"] is True
    assert len(second) == 3
    assert len(calls) == 3
    assert periodic_timestamps(Decimal("601"), 300) == [
        Decimal("0"),
        Decimal("300"),
        Decimal("600"),
    ]
