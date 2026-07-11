from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from tools.research.telegram_collector import _load_telegram_client

from .config import WSTSettings


class WSTAccessError(RuntimeError):
    pass


class WSTTelegramSource:
    """Read-only Telethon adapter. It never performs interactive authorization."""

    def __init__(self, settings: WSTSettings):
        self.settings = settings
        self.client: Any | None = None
        self.channel: Any | None = None

    async def __aenter__(self) -> WSTTelegramSource:
        if not self.settings.has_telegram_credentials:
            raise WSTAccessError("TG_API_ID and TG_API_HASH are required for the WST collector.")
        telegram_client = _load_telegram_client()
        self.client = telegram_client(
            str(self.settings.session_path), int(self.settings.api_id), self.settings.api_hash
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            self.client = None
            raise WSTAccessError(
                "Existing Telegram research session is not authorized. "
                "No login was started; authorize it manually before retrying."
            )
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    async def find_channel(self) -> Any:
        client = self._client()
        async for dialog in client.iter_dialogs():
            entity = getattr(dialog, "entity", None)
            if int(getattr(entity, "id", 0) or 0) == self.settings.channel_id:
                self.channel = entity
                return entity
        raise WSTAccessError("Канал WST не найден среди диалогов авторизованного аккаунта")

    async def get_message(self, message_id: int) -> Any | None:
        channel = self.channel or await self.find_channel()
        message = await self._client().get_messages(channel, ids=message_id)
        return message if getattr(message, "id", None) else None

    async def iter_messages(self, limit: int | None = None) -> AsyncIterator[Any]:
        channel = self.channel or await self.find_channel()
        async for message in self._client().iter_messages(channel, limit=limit):
            # reply_to is deliberately never passed: discussion comments stay out of scope.
            yield message

    def _client(self) -> Any:
        if self.client is None:
            raise WSTAccessError("Telegram source is not connected.")
        return self.client


async def doctor_channel_access(settings: WSTSettings) -> dict[str, Any]:
    """Perform the smallest allowed read-only access check without downloading media."""
    session_exists = settings.session_path.with_suffix(".session").exists()
    report: dict[str, Any] = {
        "session_file_present": session_exists,
        "authorized": False,
        "channel_found": False,
        "navigation_message_available": False,
    }
    async with WSTTelegramSource(settings) as source:
        report["authorized"] = True
        await source.find_channel()
        report["channel_found"] = True
        report["navigation_message_available"] = bool(
            await source.get_message(settings.navigation_message_id)
        )
    return report
