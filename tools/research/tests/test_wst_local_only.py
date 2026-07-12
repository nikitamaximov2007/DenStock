from __future__ import annotations

from pathlib import Path

import pytest

from tools.research.sanitize import PROJECT_ROOT
from tools.research.wst.config import DEFAULT_OUTPUT_ROOT, resolve_output_root, wst_paths


def _wst_source() -> str:
    package = Path(__file__).resolve().parents[1] / "wst"
    return "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))


def test_wst_package_has_no_cloud_storage_clients_or_upload_calls() -> None:
    source = _wst_source().lower()
    forbidden = (
        "boto" + "3",
        "botocore",
        "default_" + "storage",
        "upload_" + "file",
        "put_" + "object",
    )

    assert not any(token in source for token in forbidden)


def test_wst_processing_backends_do_not_send_media_outside_the_machine() -> None:
    package = Path(__file__).resolve().parents[1] / "wst"
    source = "\n".join(
        (package / name).read_text(encoding="utf-8")
        for name in (
            "media_pipeline.py",
            "ocr.py",
            "video_transcriber.py",
            "document_extractors.py",
        )
    ).lower()

    assert not any(token in source for token in ("requests", "http://", "https://", "upload"))


@pytest.mark.parametrize(
    "value", ("s3://bucket/wst", "bucket://wst", "https://host/wst", r"\\server\share")
)
def test_wst_output_rejects_cloud_and_network_paths(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        resolve_output_root(value, tmp_path)


def test_wst_default_output_and_all_paths_are_local(tmp_path: Path) -> None:
    allowed = (tmp_path / DEFAULT_OUTPUT_ROOT).resolve()
    assert resolve_output_root(None, tmp_path) == allowed

    class Settings:
        output_root = allowed

    assert all(path.is_relative_to(allowed) for path in wst_paths(Settings()).values())


def test_wst_default_output_root_is_project_local() -> None:
    assert (PROJECT_ROOT / DEFAULT_OUTPUT_ROOT).resolve().is_relative_to(PROJECT_ROOT.resolve())
