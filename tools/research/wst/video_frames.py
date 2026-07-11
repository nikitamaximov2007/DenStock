from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from .video_transcriber import decimal_seconds, format_timestamp


def parse_scene_timestamps(lines: Iterable[str]) -> list[Decimal]:
    values = []
    for line in lines:
        value = line.strip().split(",")[0]
        try:
            values.append(decimal_seconds(value))
        except Exception:  # noqa: BLE001 - malformed ffmpeg rows are ignored.
            continue
    return sorted(set(values))


def periodic_timestamps(duration: Decimal, every_seconds: int = 300) -> list[Decimal]:
    if duration <= 0:
        return []
    return [Decimal(value) for value in range(0, int(duration), max(1, every_seconds))]


def perceptual_hash(path: Path) -> str:
    try:
        from PIL import Image
    except ImportError:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    with Image.open(path).convert("L").resize((8, 8)) as image:
        pixels = list(image.getdata())
    average = sum(pixels) / len(pixels)
    return "".join("1" if value >= average else "0" for value in pixels)


def hamming_distance(left: str, right: str) -> int:
    if len(left) != len(right):
        return max(len(left), len(right))
    return sum(one != two for one, two in zip(left, right, strict=True))


def extract_keyframe(video_path: Path, timestamp: Decimal, output_dir: Path) -> Path:
    local = Path(__file__).resolve().parents[1] / ".runtime" / "bin" / "ffmpeg.exe"
    executable = str(local) if local.exists() else shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg is not installed.")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{format_timestamp(timestamp).replace(':', '-')}.jpg"
    subprocess.run(
        [
            executable,
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(target),
        ],
        capture_output=True,
        check=True,
    )
    return target


def retain_distinct_frames(
    frames: list[tuple[Decimal, Path]], threshold: int = 8
) -> list[dict[str, str]]:
    retained = []
    hashes: list[str] = []
    for timestamp, path in sorted(frames, key=lambda item: item[0]):
        frame_hash = perceptual_hash(path)
        if any(hamming_distance(frame_hash, prior) <= threshold for prior in hashes):
            continue
        hashes.append(frame_hash)
        retained.append(
            {
                "timestamp": str(decimal_seconds(timestamp)),
                "path": str(path),
                "perceptual_hash": frame_hash,
            }
        )
    return retained
