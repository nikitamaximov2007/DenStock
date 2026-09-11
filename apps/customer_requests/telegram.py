"""Small official-Bot-API-shaped boundary, with no live credentials required."""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from django.conf import settings

from .messengers import MessengerLinkError, consume_telegram_start


@dataclass(frozen=True, slots=True)
class TelegramStartResult:
    accepted: bool
    reply_text: str


class TelegramProvider:
    """Outbound boundary. Production wiring may implement Bot API sendMessage."""

    def send_start_acknowledgement(self, *, chat_id: str) -> None:
        """Send only after Telegram delivered a user-initiated update."""


class NoopTelegramProvider(TelegramProvider):
    """Test/development provider that never makes a network request."""

    def send_start_acknowledgement(self, *, chat_id: str) -> None:
        return None


def webhook_secret_is_valid(value: str | None) -> bool:
    configured = settings.TELEGRAM_WEBHOOK_SECRET
    return bool(configured) and hmac.compare_digest(value or "", configured)


def handle_update(
    update: object, *, provider: TelegramProvider | None = None
) -> TelegramStartResult:
    """Handle only a `/start <opaque-token>` message and expose no request data."""
    if not isinstance(update, dict):
        return TelegramStartResult(False, "")
    message = update.get("message")
    if not isinstance(message, dict):
        return TelegramStartResult(False, "")
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return TelegramStartResult(False, "")
    pieces = text.strip().split(maxsplit=1)
    if len(pieces) != 2 or pieces[0] != "/start":
        return TelegramStartResult(False, "")
    try:
        consume_telegram_start(token=pieces[1], chat_id=chat.get("id"))
    except MessengerLinkError:
        return TelegramStartResult(False, "Ссылка недействительна или уже использована.")
    (provider or NoopTelegramProvider()).send_start_acknowledgement(chat_id=str(chat["id"]))
    return TelegramStartResult(True, "Связь с заявкой подтверждена.")
