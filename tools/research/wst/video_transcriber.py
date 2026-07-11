from __future__ import annotations

import json
import shutil
import subprocess
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any


def decimal_seconds(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_DOWN)


def format_timestamp(value: Decimal | int | float | str) -> str:
    total_ms = int(decimal_seconds(value) * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def parse_ffprobe(payload: dict[str, Any]) -> dict[str, Any]:
    streams = payload.get("streams", [])
    return {
        "duration_seconds": decimal_seconds(payload.get("format", {}).get("duration", "0")),
        "audio_streams": [stream for stream in streams if stream.get("codec_type") == "audio"],
        "video_streams": [stream for stream in streams if stream.get("codec_type") == "video"],
    }


def probe_media(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is not installed.")
    completed = subprocess.run(
        [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return parse_ffprobe(json.loads(completed.stdout))


def extract_mono_audio(media_path: Path, temporary_dir: Path) -> Path:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg is not installed.")
    temporary_dir.mkdir(parents=True, exist_ok=True)
    target = temporary_dir / f"{media_path.stem}.wav"
    subprocess.run(
        [
            executable,
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        check=True,
    )
    return target


def transcription_chunks(
    duration: Decimal, chunk_seconds: int = 1800, overlap_seconds: int = 5
) -> list[tuple[Decimal, Decimal]]:
    if duration <= 0:
        return []
    chunks = []
    start = Decimal("0")
    size = Decimal(chunk_seconds)
    overlap = Decimal(overlap_seconds)
    while start < duration:
        end = min(duration, start + size)
        chunks.append((start, end))
        if end == duration:
            break
        start = end - overlap
    return chunks


def deduplicate_overlap(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[tuple[Decimal, str]] = set()
    for segment in sorted(
        segments, key=lambda item: (decimal_seconds(item["start"]), item.get("text", ""))
    ):
        key = (
            decimal_seconds(segment["start"]),
            " ".join(str(segment.get("text", "")).split()).lower(),
        )
        if key not in seen:
            seen.add(key)
            result.append(segment)
    return result


def write_transcript(
    post_id: int, duration: Decimal, segments: list[dict[str, Any]], output_dir: Path
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized = []
    for segment in deduplicate_overlap(segments):
        start, end = decimal_seconds(segment["start"]), decimal_seconds(segment["end"])
        text = " ".join(str(segment.get("text", "")).split()) or "[НЕРАЗБОРЧИВО]"
        normalized.append(
            {
                **segment,
                "start": str(start),
                "end": str(end),
                "text": text,
                "source_ref": f"wst://post/{post_id}/video?t={format_timestamp(start)}-{format_timestamp(end)}",
            }
        )
    payload = {"post_id": post_id, "duration_seconds": str(duration), "segments": normalized}
    json_path = output_dir / f"{post_id}.json"
    markdown_path = output_dir / f"{post_id}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Видео из WST",
        "",
        f"Telegram post: {post_id}",
        f"Duration: {format_timestamp(duration)}",
        "",
        "## Transcript",
        "",
    ]
    for segment in normalized:
        lines.extend(
            [
                f"[{format_timestamp(segment['start'])}-{format_timestamp(segment['end'])}]",
                segment["text"],
                segment["source_ref"],
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def transcribe_audio(
    path: Path, *, model_name: str, device: str = "auto", compute_type: str = "auto"
) -> list[dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed.") from exc
    selected_device = "cuda" if device == "auto" else device
    selected_compute_type = (
        "float16" if compute_type == "auto" and selected_device == "cuda" else compute_type
    )
    model = WhisperModel(model_name, device=selected_device, compute_type=selected_compute_type)
    segments, _info = model.transcribe(str(path), vad_filter=True, word_timestamps=False)
    return [
        {
            "start": str(decimal_seconds(item.start)),
            "end": str(decimal_seconds(item.end)),
            "text": item.text,
            "confidence": getattr(item, "avg_logprob", None),
        }
        for item in segments
    ]
