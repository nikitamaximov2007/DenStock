"""Minimal synchronous Telegram Bot API client.

Only the handful of official methods the request bot needs are used
(getUpdates, sendMessage, answerCallbackQuery, getMe, getWebhookInfo). The
client relies on the standard library on purpose: the pinned production
requirements stay unchanged and the whole network surface is one function that
tests replace with a fake.

The bot token appears only inside the request URL. It is never part of an
exception message, a log record or a stored error: every text that leaves this
module goes through ``_scrub``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

MAX_ERROR_TEXT = 200


class TelegramError(Exception):
    """Base class; ``str()`` is always safe to log and to store."""


@dataclass
class TelegramApiError(TelegramError):
    """Telegram answered and refused the call. Nothing was delivered."""

    error_code: int
    description: str
    retry_after: int | None = None

    def __str__(self) -> str:
        return f"Telegram API {self.error_code}: {self.description}"

    @property
    def retryable(self) -> bool:
        return self.error_code == 429 or self.error_code >= 500


@dataclass
class TelegramNetworkError(TelegramError):
    """No usable answer.

    ``ambiguous`` is true when the request may have reached Telegram (a read
    timeout after sending), so a resend could duplicate a customer message.
    """

    reason: str
    ambiguous: bool

    def __str__(self) -> str:
        return f"Telegram network error: {self.reason}"


def _scrub(text: object, token: str) -> str:
    value = str(text or "")
    if token:
        value = value.replace(token, "<redacted>")
    return value[:MAX_ERROR_TEXT]


class TelegramBotApi:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout: float = 15.0,
        opener=None,
    ):
        if not token:
            raise ValueError("Telegram bot token is not configured.")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def __repr__(self) -> str:  # never render the token, even in a debugger dump
        return f"TelegramBotApi(base_url={self._base_url!r})"

    def call(self, method: str, payload: dict | None = None, *, timeout: float | None = None,
             may_duplicate: bool = False):
        request = urllib.request.Request(
            f"{self._base_url}/bot{self._token}/{method}",
            data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout or self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() or b""
            if not body:
                raise TelegramApiError(exc.code, _scrub(exc.reason, self._token)) from None
        except TimeoutError:
            raise TelegramNetworkError("timeout", ambiguous=may_duplicate) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            timed_out = isinstance(reason, TimeoutError)
            raise TelegramNetworkError(
                _scrub(type(reason).__name__ if not isinstance(reason, str) else reason,
                       self._token),
                ambiguous=may_duplicate and timed_out,
            ) from None
        except OSError as exc:
            raise TelegramNetworkError(
                _scrub(type(exc).__name__, self._token), ambiguous=False
            ) from None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramNetworkError("invalid response", ambiguous=may_duplicate) from None
        if not isinstance(data, dict):
            raise TelegramNetworkError("invalid response", ambiguous=may_duplicate)
        if not data.get("ok"):
            parameters = data.get("parameters") or {}
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            raise TelegramApiError(
                int(data.get("error_code") or 0),
                _scrub(data.get("description"), self._token),
                int(retry_after) if isinstance(retry_after, int) else None,
            )
        return data.get("result")

    # --- The methods the bot uses -------------------------------------------------------

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_webhook_info(self) -> dict:
        return self.call("getWebhookInfo")

    def get_updates(self, *, offset: int, timeout: int) -> list:
        # getUpdates is read-only for Telegram; the offset makes it safe to repeat.
        return self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=timeout + 10,
        ) or []

    def send_message(self, *, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload, may_duplicate=True)

    def answer_callback_query(self, *, callback_query_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:190]
        self.call("answerCallbackQuery", payload)
