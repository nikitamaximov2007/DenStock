from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.research.wst.config import load_settings
from tools.research.wst.telegram_source import WSTAccessError, WSTTelegramSource


class _Client:
    def __init__(self, _session, _api_id, _api_hash, *, authorized=True):
        self.authorized = authorized
        self.connected = False
        self.disconnected = False
        self.start_called = False
        self.entity = SimpleNamespace(id=3278525266)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return self.authorized

    async def iter_dialogs(self):
        yield SimpleNamespace(entity=self.entity)

    async def get_messages(self, _channel, ids):
        return SimpleNamespace(id=ids)


def _settings(tmp_path: Path):
    return load_settings(
        environ={"TG_API_ID": "1", "TG_API_HASH": "hash"},
        project_root=tmp_path,
        output_root=tmp_path / "research_inputs" / "wst",
    )


def test_source_uses_existing_session_without_starting_login(monkeypatch, tmp_path: Path) -> None:
    client = _Client("", "", "")
    monkeypatch.setattr(
        "tools.research.wst.telegram_source._load_telegram_client", lambda: lambda *args: client
    )

    async def collect() -> None:
        async with WSTTelegramSource(_settings(tmp_path)) as source:
            assert (await source.find_channel()).id == 3278525266
            assert (await source.get_message(3)).id == 3

    asyncio.run(collect())

    assert client.connected and client.disconnected
    assert client.start_called is False


def test_unauthorized_existing_session_stops_without_login(monkeypatch, tmp_path: Path) -> None:
    client = _Client("", "", "", authorized=False)
    monkeypatch.setattr(
        "tools.research.wst.telegram_source._load_telegram_client", lambda: lambda *args: client
    )

    async def collect() -> None:
        async with WSTTelegramSource(_settings(tmp_path)):
            pass

    with pytest.raises(WSTAccessError, match="not authorized"):
        asyncio.run(collect())

    assert client.disconnected


def test_read_only_source_contains_no_write_operation_names() -> None:
    source = Path(__file__).parents[1] / "wst" / "telegram_source.py"
    contents = source.read_text(encoding="utf-8")
    for forbidden in (
        "send_message",
        "send_file",
        "send_reaction",
        "join_channel",
        "leave_channel",
        "forward_messages",
    ):
        assert forbidden not in contents
