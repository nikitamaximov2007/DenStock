"""Minimal synchronous MAX Bot API client.

Only the official methods the request bot needs: ``GET /me``,
``POST /messages``, ``POST /answers`` and the ``/subscriptions`` trio for the
webhook tooling. Standard library only, like the Telegram client, so the whole
network surface is one opener that tests replace with a fake server.

The token travels only in the ``Authorization`` header, never in a URL, and no
text leaving this module can contain it: every message goes through ``_scrub``.

Failures are split the way a sender must act on them:

* ``MaxApiError`` - MAX answered and refused. Nothing was delivered.
  ``retryable`` for 429 and 5xx, final for everything else (401, validation).
* ``MaxNetworkError(ambiguous=False)`` - the call certainly did not take
  effect (connection refused, DNS, a read call that timed out).
* ``MaxNetworkError(ambiguous=True)`` - a send may have reached MAX and the
  answer was lost (timeout after sending, gateway timeout, an unusable success
  body). The caller must not resend it automatically.

Transport. MAX's API certificate is issued by the Russian Trusted Root CA of
the Ministry of Digital Development, which the standard trust stores do not
contain. Verification is never switched off: ``ca_file`` names the CA this
client alone trusts, so the host's and every other client's trust stays as it
is. The client also ignores HTTP(S)_PROXY from the environment: MAX is reached
directly, never through another integration's proxy.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

MAX_ERROR_TEXT = 200
MAX_TEXT_CHARS = 4000
MAX_CALLBACK_PAYLOAD = 256
MAX_BUTTON_TEXT = 128
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{5,256}$")
# A gateway answering for MAX cannot tell whether MAX itself processed a send.
AMBIGUOUS_GATEWAY_STATUSES = frozenset({502, 504})


class MaxError(Exception):
    """Base class; ``str()`` is always safe to log and to store."""


@dataclass
class MaxApiError(MaxError):
    status: int
    code: str
    description: str
    retry_after: int | None = None

    def __str__(self) -> str:
        code = f" {self.code}" if self.code else ""
        return f"MAX API {self.status}{code}: {self.description}"

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500 or self.code == "attachment.not.ready"


@dataclass
class MaxNetworkError(MaxError):
    reason: str
    ambiguous: bool

    def __str__(self) -> str:
        return f"MAX network error: {self.reason}"


def _scrub(text: object, token: str) -> str:
    value = str(text or "")
    if token:
        value = value.replace(token, "<redacted>")
    return value[:MAX_ERROR_TEXT]


def _retry_after(headers) -> int | None:
    raw = (headers.get("Retry-After") if headers is not None else None) or ""
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return value if 0 < value <= 3600 else None


def webhook_secret_is_well_formed(value: str) -> bool:
    return bool(SECRET_RE.fullmatch(value or ""))


PEM_CERT_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+.+?-----END CERTIFICATE-----", re.DOTALL
)


def ca_fingerprints(path: str) -> list[str]:
    """SHA-256 (hex) of each certificate in a PEM file; public, safe to print."""
    text = Path(path).read_text(encoding="ascii")
    return [
        hashlib.sha256(ssl.PEM_cert_to_DER_cert(block)).hexdigest()
        for block in PEM_CERT_RE.findall(text)
    ]


def _normalized_fingerprint(value: str) -> str:
    return re.sub(r"[^0-9a-f]", "", (value or "").lower())


def tls_context(ca_file: str = "", ca_sha256: str = "") -> ssl.SSLContext:
    """A verifying TLS context; with ``ca_file`` it trusts exactly that CA.

    ``ca_sha256`` pins the file's certificate, so a swapped file is refused
    instead of silently trusted. Any problem raises ``ValueError`` before a
    single request is made.
    """
    if ca_sha256 and not ca_file:
        raise ValueError("MAX CA fingerprint is set without a MAX CA file.")
    if not ca_file:
        return ssl.create_default_context()
    try:
        fingerprints = ca_fingerprints(ca_file)
    except (OSError, UnicodeDecodeError, ValueError):
        raise ValueError("MAX CA file is missing or unreadable.") from None
    if not fingerprints:
        raise ValueError("MAX CA file contains no certificate.")
    expected = _normalized_fingerprint(ca_sha256)
    if expected and expected not in fingerprints:
        raise ValueError("MAX CA file does not match the pinned SHA-256 fingerprint.")
    try:
        context = ssl.create_default_context(cafile=ca_file)
    except ssl.SSLError:
        raise ValueError("MAX CA file is not a usable certificate.") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def direct_opener(context: ssl.SSLContext):
    """An opener that verifies with ``context`` and never uses an env proxy."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    ).open


def inline_keyboard(buttons) -> list[dict]:
    """``[[{"text", "payload"}]]`` rows as the MAX inline keyboard attachment."""
    rows = []
    for row in buttons or []:
        rows.append(
            [
                {
                    "type": "callback",
                    "text": str(button["text"])[:MAX_BUTTON_TEXT],
                    "payload": str(button["payload"]),
                }
                for button in row
            ]
        )
    if not rows:
        return []
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


class MaxBotApi:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://platform-api2.max.ru",
        timeout: float = 15.0,
        opener=None,
        ca_file: str = "",
        ca_sha256: str = "",
    ):
        if not token:
            raise ValueError("MAX bot token is not configured.")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        if opener is not None:
            self._opener = opener
            self._upload_opener = opener
        else:
            # The Bot API endpoint uses the pinned MAX CA. The official upload
            # URL is a separate iu.oneme.ru host with a public certificate.
            self._opener = direct_opener(tls_context(ca_file, ca_sha256))
            self._upload_opener = direct_opener(ssl.create_default_context())

    def __repr__(self) -> str:  # never render the token, even in a debugger dump
        return f"MaxBotApi(base_url={self._base_url!r})"

    def call(self, http_method: str, path: str, *, query: dict | None = None,
             payload: dict | None = None, may_duplicate: bool = False,
             timeout: float | None = None):
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        data = None
        headers = {"Authorization": self._token}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=http_method)
        try:
            with self._opener(request, timeout=timeout or self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, may_duplicate) from None
        except TimeoutError:
            raise MaxNetworkError("timeout", ambiguous=may_duplicate) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            timed_out = isinstance(reason, TimeoutError)
            name = reason if isinstance(reason, str) else type(reason).__name__
            raise MaxNetworkError(
                _scrub(name, self._token), ambiguous=may_duplicate and timed_out
            ) from None
        except OSError as exc:
            # Reset or broken pipe mid-exchange: the request may have been read.
            raise MaxNetworkError(
                _scrub(type(exc).__name__, self._token), ambiguous=may_duplicate
            ) from None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MaxNetworkError("invalid response", ambiguous=may_duplicate) from None
        if not isinstance(data, dict):
            raise MaxNetworkError("invalid response", ambiguous=may_duplicate)
        return data

    def _http_error(self, exc: urllib.error.HTTPError, may_duplicate: bool) -> MaxError:
        try:
            body = exc.read() or b""
        except OSError:
            body = b""
        if may_duplicate and exc.code in AMBIGUOUS_GATEWAY_STATUSES:
            return MaxNetworkError(f"gateway {exc.code}", ambiguous=True)
        code, description = "", str(exc.reason or "")
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            code = str(data.get("code") or "")
            description = str(data.get("message") or data.get("error") or description)
        return MaxApiError(
            exc.code,
            _scrub(code, self._token)[:60],
            _scrub(description, self._token),
            _retry_after(exc.headers) if exc.code == 429 or exc.code >= 500 else None,
        )

    # --- The methods the bot uses -------------------------------------------------------

    def get_me(self) -> dict:
        result = self.call("GET", "/me")
        if not isinstance(result.get("user_id"), int):
            raise MaxNetworkError("invalid response", ambiguous=False)
        return result

    def send_message(self, *, chat_id: int, text: str, buttons=None) -> dict:
        """Send one text; returns the created message. Never retried here."""
        if not text or len(text) > MAX_TEXT_CHARS:
            raise MaxApiError(400, "local.validation", "text length is out of bounds")
        payload = {"text": text, "notify": True}
        attachments = inline_keyboard(buttons)
        if attachments:
            payload["attachments"] = attachments
        result = self.call(
            "POST",
            "/messages",
            query={"chat_id": chat_id, "disable_link_preview": "true"},
            payload=payload,
            may_duplicate=True,
        )
        message = result.get("message")
        body = message.get("body") if isinstance(message, dict) else None
        mid = body.get("mid") if isinstance(body, dict) else None
        if not isinstance(mid, str) or not mid:
            # MAX said 200 but the answer is unusable: it may have been delivered.
            raise MaxNetworkError("invalid response", ambiguous=True)
        return message

    def send_file(
        self, *, chat_id: int, content: bytes, filename: str, content_type: str, caption: str = ""
    ) -> dict:
        """Upload a file through MAX /uploads, then send its attachment token."""
        token = self.upload_file(
            content=content, filename=filename, content_type=content_type
        )
        return self.send_file_token(
            chat_id=chat_id, token=token, content_type=content_type, caption=caption
        )

    def upload_file(self, *, content: bytes, filename: str, content_type: str) -> str:
        """Upload bytes and return the reusable MAX attachment token."""
        kind = "image" if content_type.startswith("image/") else "file"
        upload = self.call("POST", "/uploads", query={"type": kind})
        url = upload.get("url") if isinstance(upload, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise MaxNetworkError("invalid upload response", ambiguous=False)
        boundary = "----denstock-" + secrets.token_hex(12)
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"data\"; "
            f"filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._upload_opener(request, timeout=self._timeout) as response:
                uploaded = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MaxNetworkError(type(exc).__name__, ambiguous=True) from None
        token = uploaded.get("token") if isinstance(uploaded, dict) else None
        if not token and kind == "image" and isinstance(uploaded, dict):
            # MAX returns image tokens nested under the opaque photo key,
            # while file uploads return a top-level token.
            photos = uploaded.get("photos")
            if isinstance(photos, dict):
                tokens = [
                    value.get("token")
                    for value in photos.values()
                    if isinstance(value, dict) and isinstance(value.get("token"), str)
                ]
                if len(tokens) == 1:
                    token = tokens[0]
        if not isinstance(token, str) or not token:
            raise MaxNetworkError("invalid upload token", ambiguous=True)
        return token

    def send_file_token(
        self, *, chat_id: int, token: str, content_type: str, caption: str = ""
    ) -> dict:
        """Send a previously uploaded MAX token without creating another upload."""
        if not token:
            raise ValueError("MAX attachment token is required.")
        attachment_type = "image" if content_type.startswith("image/") else "file"
        result = self.call(
            "POST", "/messages", query={"chat_id": chat_id, "disable_link_preview": "true"},
            payload={"text": caption[:MAX_TEXT_CHARS] or " ", "notify": True,
                     "attachments": [{"type": attachment_type, "payload": {"token": token}}]},
            may_duplicate=True,
        )
        return result.get("message") or {}

    def answer_callback(
        self, *, callback_id: str, notification: str = "", message: dict | None = None
    ) -> None:
        """Answer one button press: a toast, a replacement message, or both.

        ``message`` is ``{"text": ..., "buttons": [[...]]}``; MAX replaces the
        message the button belongs to with it, which is how the selector's mark
        moves without adding anything to the conversation.
        """
        payload: dict = {}
        if notification:
            payload["notification"] = notification[:200]
        if message is not None:
            text = str(message.get("text") or "")
            if not text or len(text) > MAX_TEXT_CHARS:
                raise MaxApiError(400, "local.validation", "text length is out of bounds")
            updated: dict = {"text": text}
            attachments = inline_keyboard(message.get("buttons"))
            if attachments:
                updated["attachments"] = attachments
            payload["message"] = updated
            payload["disable_link_preview"] = True
        self._simple(
            self.call("POST", "/answers", query={"callback_id": callback_id}, payload=payload)
        )

    def list_subscriptions(self) -> list[dict]:
        result = self.call("GET", "/subscriptions")
        subscriptions = result.get("subscriptions")
        if not isinstance(subscriptions, list):
            raise MaxNetworkError("invalid response", ambiguous=False)
        return [item for item in subscriptions if isinstance(item, dict)]

    def subscribe(self, *, url: str, secret: str, update_types) -> None:
        if not url.startswith("https://"):
            raise ValueError("MAX webhook URL must be HTTPS.")
        if not webhook_secret_is_well_formed(secret):
            raise ValueError("MAX webhook secret must be 5-256 characters [A-Za-z0-9_-].")
        self._simple(
            self.call(
                "POST",
                "/subscriptions",
                payload={"url": url, "secret": secret, "update_types": list(update_types)},
            )
        )

    def unsubscribe(self, *, url: str) -> None:
        self._simple(self.call("DELETE", "/subscriptions", query={"url": url}))

    def _simple(self, result: dict) -> None:
        if result.get("success") is not True:
            raise MaxApiError(
                400, "", _scrub(result.get("message") or "request was not successful", self._token)
            )
