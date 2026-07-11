from __future__ import annotations

from pathlib import Path

import pytest

from tools.research.wst.config import load_settings, resolve_output_root


def test_wst_settings_reuse_existing_telegram_credentials_without_writing_them(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environ={"TG_API_ID": "123", "TG_API_HASH": "secret", "WST_CHANNEL_ID": "3278525266"},
        project_root=tmp_path,
        output_root=tmp_path / "research_inputs" / "wst",
    )

    assert settings.has_telegram_credentials is True
    assert settings.channel_id == 3278525266
    assert settings.navigation_message_id == 3
    assert settings.output_root == (tmp_path / "research_inputs" / "wst").resolve()


def test_wst_output_cannot_escape_ignored_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_output_root(tmp_path / "elsewhere", tmp_path)
