from __future__ import annotations

import json
import shutil
import subprocess
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from .state import WSTState


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


def extract_audio_chunk(
    audio_path: Path,
    start: Decimal,
    end: Decimal,
    temporary_dir: Path,
    chunk_number: int,
) -> Path:
    local = Path(__file__).resolve().parents[1] / ".runtime" / "bin" / "ffmpeg.exe"
    executable = str(local) if local.exists() else shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg is not installed.")
    temporary_dir.mkdir(parents=True, exist_ok=True)
    target = temporary_dir / f"{audio_path.stem}-chunk-{chunk_number:04}.wav"
    subprocess.run(
        [
            executable,
            "-y",
            "-ss",
            str(start),
            "-t",
            str(end - start),
            "-i",
            str(audio_path),
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


def transcribe_in_chunks(
    audio_path: Path,
    duration: Decimal,
    temporary_dir: Path,
    *,
    model_name: str,
    device: str,
    compute_type: str,
    chunk_seconds: int,
    overlap_seconds: int,
    state: WSTState | None = None,
    message_id: int | None = None,
    artifact_dir: Path | None = None,
    stop_after_completed: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    absolute_segments: list[dict[str, Any]] = []
    chunk_results: list[dict[str, Any]] = []
    plan = transcription_chunks(duration, chunk_seconds, overlap_seconds)
    completed_this_run = 0
    for number, (start, end) in enumerate(plan, start=1):
        stage = f"transcript_chunk_{number:04d}"
        artifact = _chunk_artifact_path(artifact_dir, message_id, number)
        prior = state.stage_record(message_id, stage) if state and message_id else None
        if prior and prior["status"] == "complete" and artifact and artifact.is_file():
            absolute_segments.extend(_read_chunk_artifact(artifact))
            chunk_results.append(
                {
                    "chunk_id": number,
                    "status": "complete",
                    "skipped_completed": True,
                    "artifact": str(artifact),
                }
            )
            continue
        if stop_after_completed is not None and completed_this_run >= stop_after_completed:
            chunk_results.extend(
                {
                    "chunk_id": pending_number,
                    "status": "pending",
                    "reason": "intentional_pause",
                }
                for pending_number in range(number, len(plan) + 1)
            )
            break
        if state and message_id:
            state.begin_stage(message_id, stage, backend="faster-whisper")
        chunk_path = extract_audio_chunk(audio_path, start, end, temporary_dir, number)
        try:
            segments = transcribe_audio(
                chunk_path, model_name=model_name, device=device, compute_type=compute_type
            )
            absolute_chunk_segments = [
                {
                    **segment,
                    "start": str(start + decimal_seconds(segment["start"])),
                    "end": str(start + decimal_seconds(segment["end"])),
                    "chunk_id": number,
                    "backend": "faster-whisper",
                    "model": model_name,
                }
                for segment in segments
            ]
            absolute_segments.extend(absolute_chunk_segments)
            if artifact:
                _write_chunk_artifact(artifact, absolute_chunk_segments)
            if state and message_id:
                state.finish_stage(
                    message_id,
                    stage,
                    backend="faster-whisper",
                    artifacts=[str(artifact)] if artifact else [],
                    diagnostics={
                        "start": str(start),
                        "end": str(end),
                        "segment_count": len(segments),
                    },
                )
            chunk_results.append(
                {"chunk_id": number, "status": "complete", "artifact": str(artifact or "")}
            )
            completed_this_run += 1
        except Exception as exc:  # noqa: BLE001
            if state and message_id:
                state.fail_stage(
                    message_id,
                    stage,
                    exc,
                    retry=True,
                    next_action="Rerun process to retry this transcript chunk locally.",
                )
            chunk_results.append(
                {
                    "chunk_id": number,
                    "status": "retry_pending",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        finally:
            chunk_path.unlink(missing_ok=True)
    return deduplicate_overlap(absolute_segments), chunk_results


def _chunk_artifact_path(
    artifact_dir: Path | None, message_id: int | None, number: int
) -> Path | None:
    if artifact_dir is None or message_id is None:
        return None
    return artifact_dir / f"post-{message_id}" / f"chunk-{number:04d}.json"


def _write_chunk_artifact(path: Path, segments: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"segments": segments}, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_chunk_artifact(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("segments") or [])
