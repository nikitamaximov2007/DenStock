from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import sha256_file
from .video_transcriber import decimal_seconds, parse_ffprobe

CommandRunner = Callable[..., subprocess.CompletedProcess]


@dataclass
class RecoveryAttempt:
    name: str
    status: str
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    stderr: str = ""
    artifact: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "command": self.command,
            "exit_code": self.exit_code,
            "stderr": self.stderr,
            "artifact": self.artifact,
        }


@dataclass
class IntegrityResult:
    valid: bool
    diagnostics: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


def executable(name: str) -> str | None:
    suffix = ".exe" if sys.platform == "win32" else ""
    local = Path(__file__).resolve().parents[1] / ".runtime" / "bin" / f"{name}{suffix}"
    return str(local) if local.exists() else shutil.which(name)


def executable_version(name: str) -> str | None:
    path = executable(name)
    if not path:
        return None
    completed = subprocess.run([path, "-version"], capture_output=True, text=True, check=False)
    return (
        (completed.stdout or completed.stderr).splitlines()[0]
        if completed.returncode == 0
        else None
    )


def validate_media_file(
    path: Path,
    *,
    expected_size: int | None = None,
    runner: CommandRunner = subprocess.run,
) -> IntegrityResult:
    diagnostics: dict[str, Any] = {
        "path_name": path.name,
        "exists": path.exists(),
        "has_part_suffix": path.suffix == ".part",
    }
    if not path.exists() or path.suffix == ".part":
        return IntegrityResult(False, diagnostics)
    actual_size = path.stat().st_size
    diagnostics.update(
        {
            "actual_size": actual_size,
            "expected_size": expected_size,
            "size_matches": expected_size in (None, 0, actual_size),
            "sha256": sha256_file(path) if actual_size else "",
            "looks_like_html": _looks_like_html(path),
        }
    )
    if not actual_size or not diagnostics["size_matches"] or diagnostics["looks_like_html"]:
        return IntegrityResult(False, diagnostics)
    probe = probe_with_profiles(path, runner=runner)
    diagnostics["probe_attempts"] = [attempt.as_dict() for attempt in probe["attempts"]]
    metadata = probe.get("metadata", {})
    diagnostics["duration_seconds"] = str(metadata.get("duration_seconds", "0"))
    diagnostics["stream_count"] = len(metadata.get("audio_streams", [])) + len(
        metadata.get("video_streams", [])
    )
    valid = bool(metadata.get("audio_streams") or metadata.get("video_streams")) and (
        decimal_seconds(metadata.get("duration_seconds", "0")) >= 0
    )
    return IntegrityResult(valid, diagnostics, metadata)


def probe_with_profiles(path: Path, *, runner: CommandRunner = subprocess.run) -> dict[str, Any]:
    ffprobe = executable("ffprobe")
    if not ffprobe:
        return {
            "metadata": {},
            "attempts": [RecoveryAttempt("ffprobe", "missing", stderr="ffprobe is not installed.")],
        }
    attempts = []
    profiles = [
        ("ffprobe_default", []),
        ("ffprobe_extended", ["-analyzeduration", "100M", "-probesize", "100M"]),
    ]
    for name, options in profiles:
        command = [
            ffprobe,
            "-v",
            "error",
            *options,
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
        completed = runner(command, capture_output=True, text=True, check=False)
        attempt = RecoveryAttempt(
            name,
            "complete" if completed.returncode == 0 else "failed",
            _safe_command(command),
            completed.returncode,
            _short_stderr(completed.stderr),
        )
        attempts.append(attempt)
        if completed.returncode == 0:
            try:
                return {
                    "metadata": parse_ffprobe(json.loads(completed.stdout)),
                    "attempts": attempts,
                }
            except json.JSONDecodeError as exc:
                attempt.status = "failed"
                attempt.stderr = f"Invalid ffprobe JSON: {exc}"
    return {"metadata": {}, "attempts": attempts}


def recover_container(
    source: Path,
    repaired_dir: Path,
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Try remux then transcode. The original file remains untouched."""
    ffmpeg = executable("ffmpeg")
    if not ffmpeg:
        return {
            "path": source,
            "attempts": [RecoveryAttempt("ffmpeg", "missing", stderr="ffmpeg missing")],
        }
    repaired_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[RecoveryAttempt] = []
    remuxed = repaired_dir / f"{source.stem}-remux.mkv"
    remux_command = [
        ffmpeg,
        "-y",
        "-err_detect",
        "ignore_err",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        str(remuxed),
    ]
    if _run_recovery_command("remux", remux_command, remuxed, attempts, runner):
        return {"path": remuxed, "attempts": attempts}
    transcoded = repaired_dir / f"{source.stem}-transcoded.mp4"
    transcode_command = [
        ffmpeg,
        "-y",
        "-err_detect",
        "ignore_err",
        "-i",
        str(source),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(transcoded),
    ]
    if _run_recovery_command("transcode", transcode_command, transcoded, attempts, runner):
        return {"path": transcoded, "attempts": attempts}
    return {"path": source, "attempts": attempts}


def extract_audio_streams(
    source: Path,
    metadata: dict[str, Any],
    output_dir: Path,
    *,
    runner: CommandRunner = subprocess.run,
) -> tuple[list[Path], list[RecoveryAttempt]]:
    ffmpeg = executable("ffmpeg")
    if not ffmpeg:
        return [], [RecoveryAttempt("audio_extract", "missing", stderr="ffmpeg missing")]
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs, attempts = [], []
    for index, _stream in enumerate(metadata.get("audio_streams", [])):
        target = output_dir / f"{source.stem}-audio-{index}.wav"
        command = [
            ffmpeg,
            "-y",
            "-err_detect",
            "ignore_err",
            "-i",
            str(source),
            "-map",
            f"0:a:{index}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
        if _run_recovery_command(f"audio_stream_{index}", command, target, attempts, runner):
            outputs.append(target)
    return outputs, attempts


def write_diagnostics(path: Path, attempts: list[RecoveryAttempt]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([attempt.as_dict() for attempt in attempts], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return path


def _run_recovery_command(
    name: str,
    command: list[str],
    target: Path,
    attempts: list[RecoveryAttempt],
    runner: CommandRunner,
) -> bool:
    target.unlink(missing_ok=True)
    completed = runner(command, capture_output=True, text=True, check=False)
    ok = completed.returncode == 0 and target.exists() and target.stat().st_size > 0
    attempts.append(
        RecoveryAttempt(
            name,
            "complete" if ok else "failed",
            _safe_command(command),
            completed.returncode,
            _short_stderr(completed.stderr),
            str(target) if ok else "",
        )
    )
    return ok


def _looks_like_html(path: Path) -> bool:
    head = path.open("rb").read(2048).lstrip().lower()
    return head.startswith(b"<html") or head.startswith(b"<!doctype html")


def _safe_command(command: list[str]) -> list[str]:
    return [
        Path(item).name if index == 0 or "/" in item or "\\" in item else item
        for index, item in enumerate(command)
    ]


def _short_stderr(value: str) -> str:
    return (value or "").strip()[-2000:]
