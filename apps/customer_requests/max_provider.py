"""MAX domain boundary. No live endpoint is assumed without official credentials."""
from __future__ import annotations

from dataclasses import dataclass

from .messengers import MessengerLinkError, consume_max_start


class MaxProvider:
    """Outbound contract for a future verified MAX integration."""

    def send_start_acknowledgement(self, *, chat_id: str) -> None:
        """Implement only after MAX credentials and official API are accepted."""


class NoopMaxProvider(MaxProvider):
    def send_start_acknowledgement(self, *, chat_id: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class MaxStartResult:
    accepted: bool


def handle_max_start(
    *, token: str, chat_id: int | str, provider: MaxProvider | None = None
) -> MaxStartResult:
    """Provider-facing core without inventing a MAX webhook or deep-link URL."""
    try:
        consume_max_start(token=token, chat_id=chat_id)
    except MessengerLinkError:
        return MaxStartResult(False)
    (provider or NoopMaxProvider()).send_start_acknowledgement(chat_id=str(chat_id))
    return MaxStartResult(True)
