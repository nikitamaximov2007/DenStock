from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tools.research.wst.config import load_settings, wst_paths
from tools.research.wst.downloader import download_media, safe_media_name


class _DownloadClient:
    async def download_media(self, _message, file: str):
        Path(file).write_bytes(b"media")
        return file


class _FloodWaitClient:
    async def download_media(self, _message, file: str):
        error = RuntimeError("wait")
        error.seconds = 30
        raise error


def test_safe_media_name_keeps_extension_and_removes_path_traversal() -> None:
    name = safe_media_name(12, "../../bad name?.pdf", "application/pdf")
    assert name == "post-12-bad-name.pdf"


def test_downloader_uses_part_then_atomic_final_file(tmp_path: Path) -> None:
    settings = load_settings(
        project_root=tmp_path, output_root=tmp_path / "research_inputs" / "wst"
    )
    paths = wst_paths(settings)
    result = asyncio.run(
        download_media(
            _DownloadClient(),
            SimpleNamespace(id=12),
            {"media_type": "document", "file_name": "slides.pdf", "mime_type": "application/pdf"},
            paths,
        )
    )

    assert result["status"] == "downloaded"
    assert Path(result["path"]).exists()
    assert not Path(result["path"] + ".part").exists()


def test_downloader_skips_existing_matching_hash(tmp_path: Path) -> None:
    settings = load_settings(
        project_root=tmp_path, output_root=tmp_path / "research_inputs" / "wst"
    )
    paths = wst_paths(settings)
    first = asyncio.run(
        download_media(
            _DownloadClient(),
            SimpleNamespace(id=12),
            {"media_type": "audio", "file_name": "a.mp3", "mime_type": "audio/mpeg"},
            paths,
        )
    )
    second = asyncio.run(
        download_media(
            _DownloadClient(),
            SimpleNamespace(id=12),
            {"media_type": "audio", "file_name": "a.mp3", "mime_type": "audio/mpeg"},
            paths,
            existing_sha256=first["sha256"],
        )
    )
    assert second["status"] == "skipped"


def test_downloader_records_flood_wait_without_retrying_aggressively(tmp_path: Path) -> None:
    settings = load_settings(
        project_root=tmp_path,
        output_root=tmp_path / "research_inputs" / "wst",
    )
    result = asyncio.run(
        download_media(
            _FloodWaitClient(),
            SimpleNamespace(id=12),
            {"media_type": "audio", "file_name": "a.mp3", "mime_type": "audio/mpeg"},
            wst_paths(settings),
        )
    )

    assert result == {"status": "flood_wait", "error": "FloodWait: retry after 30s"}
