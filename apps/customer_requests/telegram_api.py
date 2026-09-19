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
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

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


def validated_proxy_url(value: str) -> str:
    """An explicit HTTP CONNECT proxy for the Bot API, or "" for a direct connection.

    Only ``http://host:port`` is accepted: no credentials, path or query. The
    proxy only tunnels, so TLS to api.telegram.org (and the token inside the
    request path) stays end to end. The rejected value is never echoed.
    """
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("Telegram proxy URL is invalid.") from None
    if (
        parts.scheme != "http"
        or not parts.hostname
        or port is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Telegram proxy URL is invalid.")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"http://{host}:{port}"


class TelegramBotApi:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout: float = 15.0,
        opener=None,
        proxy_url: str = "",
    ):
        if not token:
            raise ValueError("Telegram bot token is not configured.")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._proxy_url = validated_proxy_url(proxy_url)
        if opener is not None:
            self._opener = opener
        elif self._proxy_url:
            # Only this client's Bot API calls use the proxy (CONNECT tunnel);
            # certificate verification of api.telegram.org stays in urllib.
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": self._proxy_url, "http": self._proxy_url})
            ).open
        else:
            self._opener = urllib.request.urlopen

    def __repr__(self) -> str:  # never render the token, even in a debugger dump
        return f"TelegramBotApi(base_url={self._base_url!r}, proxy={bool(self._proxy_url)})"

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
        if data.get("ok") is not True:
            parameters = data.get("parameters") or {}
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            error_code = data.get("error_code")
            if not isinstance(error_code, int) or isinstance(error_code, bool):
                raise TelegramNetworkError("invalid response", ambiguous=may_duplicate)
            raise TelegramApiError(
                error_code,
                _scrub(data.get("description"), self._token),
                retry_after
                if isinstance(retry_after, int) and not isinstance(retry_after, bool)
                and 0 < retry_after <= 3600
                else None,
            )
        return data.get("result")

    # --- The methods the bot uses -------------------------------------------------------

    def get_me(self) -> dict:
        return self._object_result("getMe")

    def get_webhook_info(self) -> dict:
        return self._object_result("getWebhookInfo")

    def _object_result(self, method: str) -> dict:
        result = self.call(method)
        if not isinstance(result, dict):
            # An unusable answer at startup is an outage, not a crash.
            raise TelegramNetworkError("invalid response", ambiguous=False)
        return result

    def get_updates(self, *, offset: int, timeout: int) -> list:
        # getUpdates is read-only for Telegram; the offset makes it safe to repeat.
        result = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=timeout + 10,
        )
        if result is None:
            return []
        if not isinstance(result, list):
            raise TelegramNetworkError("invalid response", ambiguous=False)
        return result

    def send_message(self, *, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = self.call("sendMessage", payload, may_duplicate=True)
        if not isinstance(result, dict):
            # Telegram said ok but the answer is unusable: it may have been delivered.
            raise TelegramNetworkError("invalid response", ambiguous=True)
        return result

    def send_file(
        self, *, chat_id: int, content: bytes, filename: str, content_type: str, caption: str = ""
    ) -> dict:
        """Send a validated document/photo using Telegram's multipart Bot API."""
        method = "sendPhoto" if content_type.startswith("image/") else "sendDocument"
        field = "photo" if method == "sendPhoto" else "document"
        boundary = "----denstock-" + secrets.token_hex(12)
        parts = []
        for name, value in (("chat_id", str(chat_id)), ("caption", caption[:1024])):
            parts.extend([
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{value}\r\n"
                ).encode()
            ])
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n".encode() + content + b"\r\n"
        )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"{self._base_url}/bot{self._token}/{method}", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST"
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read()
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise TelegramNetworkError(
                _scrub(type(exc).__name__, self._token), ambiguous=True
            ) from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramNetworkError("invalid response", ambiguous=True) from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise TelegramApiError(
                int(data.get("error_code") or 400),
                _scrub(data.get("description"), self._token),
            )
        return data.get("result") or {}

    def edit_message_text(
        self, *, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None
    ) -> None:
        """Re-render a message the bot already sent (the selector's ✓ marker).

        Purely cosmetic: the customer's choice is already stored, so a refusal
        here is logged by the caller and changes nothing.
        """
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.call("editMessageText", payload)

    def answer_callback_query(self, *, callback_query_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:190]
        self.call("answerCallbackQuery", payload)
