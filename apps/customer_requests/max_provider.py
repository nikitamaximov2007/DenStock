"""MAX domain boundary.

The official API contract is verified and written down in
``docs/design/customer-request-max.md``; what is still absent here is the live
transport, not the knowledge of it. ``MaxProvider`` stays the seam a real
client will implement, so the domain keeps working against a fake until then.
"""
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
