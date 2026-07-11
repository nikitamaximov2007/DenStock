from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .media_recovery import executable_version


def runtime_root() -> Path:
    return Path(__file__).resolve().parents[1] / ".runtime"


def runtime_bin() -> Path:
    path = runtime_root() / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def discover_executable(name: str) -> str | None:
    local = runtime_bin() / (f"{name}.exe" if sys.platform == "win32" else name)
    return str(local) if local.exists() else shutil.which(name)


def install_portable_ffmpeg() -> bool:
    """Fetch a public static FFmpeg build into ignored user-space runtime storage."""
    destination = runtime_bin()
    archive = runtime_root() / "ffmpeg-portable.zip"
    url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    try:
        urllib.request.urlretrieve(url, archive)
        with zipfile.ZipFile(archive) as package:
            members = [
                name
                for name in package.namelist()
                if name.endswith(("/ffmpeg.exe", "/ffprobe.exe"))
            ]
            for member in members:
                target = destination / Path(member).name
                with package.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        return (destination / "ffmpeg.exe").exists() and (destination / "ffprobe.exe").exists()
    finally:
        archive.unlink(missing_ok=True)


def bootstrap_media(
    *,
    install: bool = False,
    whisper_model: str = "large-v3",
    download_model: bool = False,
) -> dict[str, Any]:
    """Prepare only user-space tooling. Installs require an explicit CLI flag."""
    research_root = Path(__file__).resolve().parents[1]
    requirements = research_root / "requirements-wst.txt"
    report: dict[str, Any] = {
        "python": sys.executable,
        "requirements": str(requirements.name),
        "actions": [],
        "manual_steps": [],
    }
    if install:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
            capture_output=True,
            text=True,
            check=False,
        )
        report["actions"].append({"name": "python_dependencies", "exit_code": completed.returncode})
    ffmpeg = discover_executable("ffmpeg")
    ffprobe = discover_executable("ffprobe")
    if install and (not ffmpeg or not ffprobe) and shutil.which("winget"):
        completed = subprocess.run(
            [
                "winget",
                "install",
                "--id",
                "Gyan.FFmpeg.Shared",
                "--exact",
                "--scope",
                "user",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        report["actions"].append({"name": "ffmpeg_winget", "exit_code": completed.returncode})
        ffmpeg, ffprobe = discover_executable("ffmpeg"), discover_executable("ffprobe")
    if install and (not ffmpeg or not ffprobe):
        report["actions"].append({"name": "ffmpeg_portable", "ok": install_portable_ffmpeg()})
        ffmpeg, ffprobe = discover_executable("ffmpeg"), discover_executable("ffprobe")
    if not ffmpeg or not ffprobe:
        report["manual_steps"].append(
            "Install FFmpeg for the current user, then ensure ffmpeg.exe and ffprobe.exe are "
            "on PATH or tools/research/.runtime/bin/."
        )
    tesseract = discover_executable("tesseract")
    if install and not tesseract and shutil.which("winget"):
        completed = subprocess.run(
            [
                "winget",
                "install",
                "--id",
                "UB-Mannheim.TesseractOCR",
                "--exact",
                "--scope",
                "user",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        report["actions"].append({"name": "tesseract_winget", "exit_code": completed.returncode})
        tesseract = discover_executable("tesseract")
    if not tesseract:
        report["manual_steps"].append(
            "Install Tesseract OCR with rus and eng language data for the current user."
        )
    report.update(
        {
            "ffmpeg": ffmpeg is not None,
            "ffprobe": ffprobe is not None,
            "ffmpeg_version": executable_version("ffmpeg") if ffmpeg else None,
            "ffprobe_version": executable_version("ffprobe") if ffprobe else None,
            "tesseract": tesseract is not None,
        }
    )
    try:
        from faster_whisper import WhisperModel

        report["faster_whisper"] = True
        if download_model:
            WhisperModel(whisper_model, device="cpu", compute_type="int8")
            report["whisper_model_ready"] = whisper_model
        else:
            report["whisper_model_ready"] = False
    except ImportError:
        report["faster_whisper"] = False
        report["whisper_model_ready"] = False
    return report
