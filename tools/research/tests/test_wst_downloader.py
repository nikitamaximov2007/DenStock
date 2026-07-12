from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tools.research.wst.config import load_settings, wst_paths
from tools.research.wst.downloader import download_media, safe_media_name
from tools.research.wst.state import WSTState


class _IterDownloadClient:
    def __init__(self, payload: bytes, *, fail_after: int | None = None) -> None:
        self.payload = payload
        self.fail_after = fail_after
        self.offsets: list[int] = []

    async def iter_download(self, _media, *, offset: int, **_kwargs):
        self.offsets.append(offset)
        sent = offset
        while sent < len(self.payload):
            chunk = self.payload[sent : sent + 2]
            yield chunk
            sent += len(chunk)
            if self.fail_after is not None and sent >= self.fail_after:
                raise ConnectionError("test interruption")


class _FloodWaitClient:
    async def iter_download(self, _media, **_kwargs):
        error = RuntimeError("wait")
        error.seconds = 30
        raise error
        yield b""  # pragma: no cover - async generator marker


def _settings(tmp_path: Path):
    return load_settings(project_root=tmp_path, output_root=tmp_path / "research_inputs" / "wst")


def _message(message_id: int = 12) -> SimpleNamespace:
    return SimpleNamespace(id=message_id, media="telegram-media")


def _media(size: int) -> dict[str, object]:
    return {
        "media_type": "document",
        "file_name": "slides.pdf",
        "mime_type": "application/pdf",
        "size_bytes": size,
    }


def test_safe_media_name_keeps_extension_and_removes_path_traversal() -> None:
    name = safe_media_name(12, "../../bad name?.pdf", "application/pdf")
    assert name == "post-12-bad-name.pdf"


def test_downloader_uses_part_then_atomic_final_file(tmp_path: Path) -> None:
    paths = wst_paths(_settings(tmp_path))
    result = asyncio.run(
        download_media(_IterDownloadClient(b"media"), _message(), _media(5), paths)
    )

    assert result["status"] == "downloaded"
    assert Path(result["path"]).read_bytes() == b"media"
    assert not Path(result["path"] + ".part").exists()


def test_downloader_resumes_from_verified_part_and_keeps_prefix(tmp_path: Path) -> None:
    paths = wst_paths(_settings(tmp_path))
    state_path = paths["state"] / "wst_state.sqlite3"
    with WSTState(state_path) as state:
        first = asyncio.run(
            download_media(
                _IterDownloadClient(b"abcdefgh", fail_after=4),
                _message(),
                _media(8),
                paths,
                retries=0,
                state=state,
                checkpoint_bytes=2,
            )
        )
        record = state.download_record(12)
        second_client = _IterDownloadClient(b"abcdefgh")
        second = asyncio.run(
            download_media(
                second_client,
                _message(),
                _media(8),
                paths,
                state=state,
                checkpoint_bytes=2,
            )
        )

    assert first["status"] == "retry_pending"
    assert record is not None and record["last_successful_offset"] == 4
    assert second["status"] == "downloaded"
    assert second_client.offsets == [4]
    assert Path(second["path"]).read_bytes() == b"abcdefgh"


def test_downloader_skips_existing_matching_hash(tmp_path: Path) -> None:
    paths = wst_paths(_settings(tmp_path))
    first = asyncio.run(download_media(_IterDownloadClient(b"media"), _message(), _media(5), paths))
    second = asyncio.run(
        download_media(
            _IterDownloadClient(b"other"),
            _message(),
            _media(5),
            paths,
            existing_sha256=first["sha256"],
        )
    )
    assert second["status"] == "skipped"


def test_downloader_records_flood_wait_without_retrying_aggressively(tmp_path: Path) -> None:
    paths = wst_paths(_settings(tmp_path))
    result = asyncio.run(download_media(_FloodWaitClient(), _message(), _media(5), paths))

    assert result["status"] == "retry_pending"
    assert result["error"] == "FloodWait: retry after 30s"


def test_downloader_retries_empty_file_as_integrity_failure(tmp_path: Path) -> None:
    paths = wst_paths(_settings(tmp_path))
    result = asyncio.run(
        download_media(_IterDownloadClient(b""), _message(), _media(10), paths, retries=0)
    )

    assert result["status"] == "retry_pending"
    assert "empty file" in result["error"]
