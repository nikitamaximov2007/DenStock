from __future__ import annotations

from pathlib import Path
from typing import Any

from .media_recovery import (
    extract_audio_streams,
    recover_container,
    validate_media_file,
    write_diagnostics,
)
from .ocr import active_ocr_backend, run_ocr, write_ocr
from .state import WSTState
from .video_frames import extract_keyframe, periodic_timestamps, retain_distinct_frames
from .video_transcriber import transcribe_in_chunks, write_transcript


def process_media_record(
    item: dict[str, Any],
    paths: dict[str, Path],
    state: WSTState,
    *,
    whisper_model: str,
    device: str,
    compute_type: str,
    ocr_languages: str,
    chunk_seconds: int = 900,
    overlap_seconds: int = 20,
) -> dict[str, Any]:
    """Recover independent audio and visual evidence without deleting successful stages."""
    message_id = int(item["message_id"])
    raw_path = str(item.get("path") or "").strip()
    source = Path(raw_path) if raw_path else None
    if source is None or not source.is_file():
        state.begin_stage(message_id, "downloaded")
        state.fail_stage(
            message_id,
            "downloaded",
            "Local media path is missing.",
            retry=True,
            next_action=(
                "Run retry-media --only-download-failed after re-downloading from Telegram."
            ),
        )
        return {"message_id": message_id, "status": "retry_pending", "reason": "missing_file"}

    state.begin_stage(message_id, "integrity_checked", backend="ffprobe")
    integrity = validate_media_file(source, expected_size=item.get("size_bytes"))
    attempts = []
    working = source
    if not integrity.valid:
        repaired = recover_container(source, paths["repaired"])
        attempts.extend(repaired["attempts"])
        working = repaired["path"]
        integrity = validate_media_file(working, expected_size=None)
        state.finish_stage(
            message_id,
            "container_repaired",
            status="complete" if working != source else "retry_pending",
            backend="ffmpeg",
            artifacts=[str(working)] if working != source else [],
            diagnostics={"attempts": [attempt.as_dict() for attempt in attempts]},
            next_action="Use retry-media --only-probe-failed --force-repair."
            if working == source
            else "",
        )
    diagnostics_path = write_diagnostics(
        paths["diagnostics"] / f"post-{message_id}-recovery.json", attempts
    )
    if not integrity.valid:
        state.fail_stage(
            message_id,
            "integrity_checked",
            "Container did not pass integrity and recovery checks.",
            diagnostics=integrity.diagnostics,
            artifacts=[str(diagnostics_path)],
            retry=True,
            next_action="Re-download the Telegram media, then run retry-media --only-probe-failed.",
        )
        return {"message_id": message_id, "status": "retry_pending", "reason": "integrity"}
    state.finish_stage(
        message_id,
        "integrity_checked",
        backend="ffprobe",
        diagnostics=integrity.diagnostics,
        artifacts=[str(working), str(diagnostics_path)],
        content_hash=integrity.diagnostics.get("sha256"),
    )
    state.finish_stage(
        message_id, "metadata_checked", backend="ffprobe", diagnostics=integrity.metadata
    )

    visual = _process_visual(message_id, working, integrity.metadata, paths, ocr_languages, state)
    audio = _process_audio(
        message_id,
        working,
        integrity.metadata,
        paths,
        whisper_model,
        device,
        compute_type,
        state,
        chunk_seconds,
        overlap_seconds,
    )
    status = "complete" if visual["complete"] and audio["complete"] else "partial"
    state.finish_stage(
        message_id,
        "evidence_built",
        status=status,
        artifacts=visual["artifacts"] + audio["artifacts"],
        diagnostics={"visual": visual, "audio": audio},
        next_action="Run retry-media for pending stages." if status == "partial" else "",
    )
    return {"message_id": message_id, "status": status, "visual": visual, "audio": audio}


def _process_audio(
    message_id: int,
    source: Path,
    metadata: dict[str, Any],
    paths: dict[str, Path],
    whisper_model: str,
    device: str,
    compute_type: str,
    state: WSTState,
    chunk_seconds: int,
    overlap_seconds: int,
) -> dict[str, Any]:
    state.begin_stage(message_id, "audio_extracted", backend="ffmpeg")
    if not metadata.get("audio_streams"):
        state.finish_stage(
            message_id,
            "audio_extracted",
            status="partial",
            diagnostics={"reason": "no_audio_stream"},
            next_action=(
                "Visual-only evidence retained; no audio recovery is possible for this source."
            ),
        )
        return {"complete": False, "reason": "no_audio_stream", "artifacts": []}
    audio_paths, attempts = extract_audio_streams(source, metadata, paths["state"] / "tmp")
    if not audio_paths:
        state.fail_stage(
            message_id,
            "audio_extracted",
            "No audio stream could be extracted.",
            diagnostics={"attempts": [attempt.as_dict() for attempt in attempts]},
            retry=True,
            next_action="Run retry-media --only-audio-failed --force-repair.",
        )
        return {"complete": False, "reason": "audio_extract", "artifacts": []}
    state.finish_stage(
        message_id,
        "audio_extracted",
        backend="ffmpeg",
        artifacts=[str(path) for path in audio_paths],
        diagnostics={"attempts": [attempt.as_dict() for attempt in attempts]},
    )
    state.begin_stage(message_id, "transcript_created", backend="faster-whisper")
    errors = []
    for audio_path in audio_paths:
        try:
            segments, chunk_results = transcribe_in_chunks(
                audio_path,
                metadata["duration_seconds"],
                paths["state"] / "tmp",
                model_name=whisper_model,
                device=device,
                compute_type=compute_type,
                chunk_seconds=chunk_seconds,
                overlap_seconds=overlap_seconds,
            )
            failed_chunks = [item for item in chunk_results if item["status"] != "complete"]
            json_path, markdown_path = write_transcript(
                message_id, metadata["duration_seconds"], segments, paths["transcripts"]
            )
            state.finish_stage(
                message_id,
                "transcript_created",
                status="partial" if failed_chunks else "complete",
                backend="faster-whisper",
                artifacts=[str(json_path), str(markdown_path)],
                diagnostics={
                    "segment_count": len(segments),
                    "audio_stream": audio_paths.index(audio_path),
                    "chunks": chunk_results,
                },
                next_action="Run retry-media --only-transcript-failed --retry-failed-chunks."
                if failed_chunks
                else "",
            )
            return {
                "complete": not failed_chunks,
                "artifacts": [str(json_path), str(markdown_path)],
                "failed_chunks": len(failed_chunks),
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{exc.__class__.__name__}: {exc}")
    state.fail_stage(
        message_id,
        "transcript_created",
        "All local transcription attempts failed.",
        diagnostics={"backend_attempts": errors},
        artifacts=[str(path) for path in audio_paths],
        retry=True,
        next_action=(
            "Install faster-whisper or whisper.cpp, then run retry-media --only-transcript-failed."
        ),
    )
    return {
        "complete": False,
        "reason": "transcription",
        "artifacts": [str(path) for path in audio_paths],
    }


def _process_visual(
    message_id: int,
    source: Path,
    metadata: dict[str, Any],
    paths: dict[str, Path],
    languages: str,
    state: WSTState,
) -> dict[str, Any]:
    state.begin_stage(message_id, "frames_extracted", backend="ffmpeg-periodic")
    if not metadata.get("video_streams"):
        state.finish_stage(
            message_id,
            "frames_extracted",
            status="partial",
            diagnostics={"reason": "no_video_stream"},
        )
        return {"complete": False, "reason": "no_video_stream", "artifacts": []}
    frame_dir = paths["keyframes"] / str(message_id)
    extracted = []
    errors = []
    for timestamp in periodic_timestamps(metadata["duration_seconds"], every_seconds=30):
        try:
            extracted.append((timestamp, extract_keyframe(source, timestamp, frame_dir)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{timestamp}: {exc.__class__.__name__}")
    if not extracted:
        state.fail_stage(
            message_id,
            "frames_extracted",
            "No keyframe could be decoded from the media.",
            diagnostics={"errors": errors},
            retry=True,
            next_action="Run retry-media --only-ocr-failed --force-repair.",
        )
        return {"complete": False, "reason": "frames", "artifacts": []}
    frames = retain_distinct_frames(extracted)
    state.finish_stage(
        message_id,
        "frames_extracted",
        backend="ffmpeg-periodic",
        artifacts=[item["path"] for item in frames],
        diagnostics={"frame_count": len(frames), "decode_errors": errors},
    )
    backend = active_ocr_backend() or "unavailable"
    state.begin_stage(message_id, "ocr_created", backend=backend)
    ocr_frames, ocr_errors = [], []
    for frame in frames:
        try:
            ocr_frames.append(
                {
                    **frame,
                    **run_ocr(Path(frame["path"]), languages),
                    "source_ref": f"wst://post/{message_id}/frame?t={frame['timestamp']}",
                }
            )
        except Exception as exc:  # noqa: BLE001
            ocr_errors.append(f"{frame['timestamp']}: {exc.__class__.__name__}: {exc}")
    json_path, markdown_path = write_ocr(message_id, ocr_frames, paths["ocr"])
    if ocr_errors:
        state.fail_stage(
            message_id,
            "ocr_created",
            "OCR is incomplete; retained frames are ready for a retry.",
            diagnostics={"errors": ocr_errors, "ocr_blocks": len(ocr_frames)},
            artifacts=[str(json_path), str(markdown_path), *[item["path"] for item in frames]],
            retry=True,
            next_action="Run bootstrap-media --install, then retry-media --only-ocr-failed.",
        )
    else:
        state.finish_stage(
            message_id,
            "ocr_created",
            backend=backend,
            artifacts=[str(json_path), str(markdown_path)],
            diagnostics={"ocr_blocks": len(ocr_frames)},
        )
    return {
        "complete": not ocr_errors,
        "artifacts": [str(json_path), str(markdown_path)],
        "frame_count": len(frames),
        "ocr_blocks": len(ocr_frames),
    }
