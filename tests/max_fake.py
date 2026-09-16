"""A local stand-in for MAX: its Bot API over real HTTP, its webhook, its deep links.

Nothing here reaches the internet and no real credential exists. Three parts:

* ``FakeMaxServer`` - a threaded HTTP server speaking the subset of the MAX
  Bot API the bot uses (``GET /me``, ``POST /messages``, ``POST /answers``,
  ``GET|POST|DELETE /subscriptions``). Each call can be scripted to fail the
  ways the real network does: 401, 429, 5xx, a slow answer, a dropped
  connection, malformed JSON, or a send that is accepted and then never
  answered (the ambiguous case).
* update builders shaped like MAX webhook bodies, and ``deliver`` to post one
  to Django with (or without) the webhook secret header;
* ``FakeDeepLinkServer`` - answers ``/<bot>?start=<payload>`` with a page, so
  a real browser can prove the success-page redirect chain lands there.

Run standalone for a browser session::

    python -m tests.max_fake --api-port 18780 --deep-link-port 18781
"""
from __future__ import annotations

import argparse
import itertools
import json
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Deliberately not token-shaped: no digits-colon prefix, marked as fake.
FAKE_MAX_TOKEN = "fake-max-token-for-local-tests-only"
FAKE_WEBHOOK_SECRET = "fake_max_webhook_secret_local_only"
FAKE_BOT_USERNAME = "id0000000000_bot"
FAKE_BOT_USER_ID = 900000001

_numbers = itertools.count(1)


def _next() -> int:
    return next(_numbers)


# --- Webhook update builders ------------------------------------------------------------


def _user(user_id: int) -> dict:
    return {
        "user_id": user_id,
        "first_name": "Клиент",
        "last_name": None,
        "username": None,
        "is_bot": False,
        "last_activity_time": 1758000000000,
    }


def bot_started(user_id: int, chat_id: int, payload: str | None = None, *, timestamp=None) -> dict:
    update = {
        "update_type": "bot_started",
        "timestamp": timestamp or 1758000000000 + _next(),
        "chat_id": chat_id,
        "user": _user(user_id),
        "user_locale": "ru",
    }
    if payload is not None:
        update["payload"] = payload
    return update


def message_created(
    user_id: int,
    chat_id: int,
    text: str | None,
    *,
    mid: str | None = None,
    chat_type: str = "dialog",
    is_bot: bool = False,
) -> dict:
    number = _next()
    sender = _user(user_id)
    sender["is_bot"] = is_bot
    return {
        "update_type": "message_created",
        "timestamp": 1758000000000 + number,
        "message": {
            "sender": sender,
            "recipient": {"chat_id": chat_id, "chat_type": chat_type, "user_id": None},
            "timestamp": 1758000000000 + number,
            "body": {
                "mid": mid or f"mid.{number:016x}{user_id:08x}",
                "seq": number,
                "text": text,
                "attachments": None,
            },
        },
        "user_locale": "ru",
    }


def message_callback(
    user_id: int, chat_id: int | None, payload: str, *, callback_id: str | None = None
) -> dict:
    number = _next()
    return {
        "update_type": "message_callback",
        "timestamp": 1758000000000 + number,
        "callback": {
            "timestamp": 1758000000000 + number,
            "callback_id": callback_id or f"cb.{number:012x}",
            "payload": payload,
            "user": _user(user_id),
        },
        "message": (
            None
            if chat_id is None
            else {
                "recipient": {"chat_id": chat_id, "chat_type": "dialog", "user_id": None},
                "body": {"mid": f"mid.bot{number:012x}", "seq": number, "text": "..."},
                "timestamp": 1758000000000 + number,
            }
        ),
        "user_locale": "ru",
    }


WEBHOOK_PATH = "/customer-requests/max/webhook/"


def deliver(client, update, *, secret: str | None = FAKE_WEBHOOK_SECRET, raw: bytes | None = None):
    """POST one update to the Django webhook as MAX would."""
    headers = {}
    if secret is not None:
        headers["HTTP_X_MAX_BOT_API_SECRET"] = secret
    body = raw if raw is not None else json.dumps(update, ensure_ascii=False).encode()
    return client.post(WEBHOOK_PATH, data=body, content_type="application/json", **headers)


# --- Fake Bot API server ------------------------------------------------------------------


class FakeMaxServer:
    """Scriptable MAX Bot API on 127.0.0.1.

    ``script(path, *behaviours)`` queues one-shot behaviours for the next calls
    to ``path`` (``"/messages"`` etc.); an empty queue answers normally:

    * ``("status", code, body_dict, headers)`` - an HTTP error answer;
    * ``("delay", seconds)`` - answer normally after a pause;
    * ``("drop",)`` - close the connection without an answer, nothing stored;
    * ``("malformed",)`` - HTTP 200 with a body that is not JSON;
    * ``("empty_success",)`` - HTTP 200 ``{}``: success with an unusable body;
    * ``("accept_then_hang", seconds)`` - store the message, then stall past
      the client's timeout: MAX has it, the client never learns.
    """

    def __init__(self, token: str = FAKE_MAX_TOKEN):
        self.token = token
        self.lock = threading.Lock()
        self.scripts: dict[str, deque] = defaultdict(deque)
        self.requests: list[dict] = []
        self.sent: list[dict] = []
        self.answers: list[dict] = []
        self.subscriptions: list[dict] = []
        self._server = None
        self._thread = None

    # lifecycle
    def start(self, port: int = 0) -> str:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                return

            def do_GET(self):
                server._handle(self, "GET")

            def do_POST(self):
                server._handle(self, "POST")

            def do_DELETE(self):
                server._handle(self, "DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def script(self, path: str, *behaviours) -> None:
        with self.lock:
            self.scripts[path].extend(behaviours)

    # inspection
    def texts_to(self, chat_id: int) -> list[str]:
        with self.lock:
            return [item["text"] for item in self.sent if item["chat_id"] == chat_id]

    def all_requests_text(self) -> str:
        with self.lock:
            return json.dumps(self.requests, ensure_ascii=False)

    # handling
    def _reply(self, handler, code: int, body, headers=None) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(raw)

    def _handle(self, handler, method: str) -> None:
        parts = urlsplit(handler.path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode()) if raw else None
        except ValueError:
            body = None
        with self.lock:
            self.requests.append(
                {
                    "method": method,
                    "path": parts.path,
                    "query": query,
                    "authorization": handler.headers.get("Authorization"),
                    "url": handler.path,
                    "body": body,
                }
            )
            behaviour = self.scripts[parts.path].popleft() if self.scripts[parts.path] else None
        if handler.headers.get("Authorization") != self.token:
            return self._reply(
                handler, 401, {"code": "verify.token", "message": "Invalid access_token"}
            )
        if behaviour:
            kind = behaviour[0]
            if kind == "status":
                _, code, payload, *rest = behaviour
                return self._reply(handler, code, payload, rest[0] if rest else None)
            if kind == "delay":
                time.sleep(behaviour[1])
            elif kind == "drop":
                handler.close_connection = True
                handler.connection.close()
                return None
            elif kind == "malformed":
                return self._reply(handler, 200, b"{not json")
            elif kind == "empty_success":
                return self._reply(handler, 200, {})
            elif kind == "accept_then_hang":
                result = self._route(method, parts.path, query, body)
                time.sleep(behaviour[1])
                return self._reply(handler, 200, result[1])
        code, payload = self._route(method, parts.path, query, body)
        return self._reply(handler, code, payload)

    def _route(self, method, path, query, body):
        if method == "GET" and path == "/me":
            return 200, {
                "user_id": FAKE_BOT_USER_ID,
                "first_name": "PRO-STOR",
                "username": FAKE_BOT_USERNAME,
                "is_bot": True,
                "last_activity_time": 1758000000000,
            }
        if method == "POST" and path == "/messages":
            if not isinstance(body, dict) or not isinstance(body.get("text"), str):
                return 400, {"code": "proto.payload", "message": "text is required"}
            if len(body["text"]) > 4000:
                return 400, {"code": "proto.payload", "message": "text: size must be <= 4000"}
            try:
                chat_id = int(query["chat_id"])
            except (KeyError, ValueError):
                return 400, {"code": "proto.payload", "message": "chat_id is required"}
            number = _next()
            mid = f"mid.sent{number:016x}"
            with self.lock:
                self.sent.append(
                    {
                        "chat_id": chat_id,
                        "text": body["text"],
                        "attachments": body.get("attachments"),
                        "mid": mid,
                    }
                )
            return 200, {
                "message": {
                    "recipient": {"chat_id": chat_id, "chat_type": "dialog", "user_id": None},
                    "timestamp": 1758000000000 + number,
                    "body": {"mid": mid, "seq": number, "text": body["text"]},
                }
            }
        if method == "POST" and path == "/answers":
            with self.lock:
                self.answers.append({"callback_id": query.get("callback_id"), "body": body})
            return 200, {"success": True}
        if path == "/subscriptions":
            with self.lock:
                if method == "GET":
                    return 200, {"subscriptions": list(self.subscriptions)}
                if method == "POST":
                    self.subscriptions = [
                        item for item in self.subscriptions if item["url"] != body.get("url")
                    ]
                    self.subscriptions.append(
                        {
                            "url": body.get("url"),
                            "time": 1758000000000,
                            "update_types": body.get("update_types") or [],
                            "version": "0.0.1",
                        }
                    )
                    return 200, {"success": True}
                if method == "DELETE":
                    self.subscriptions = [
                        item for item in self.subscriptions if item["url"] != query.get("url")
                    ]
                    return 200, {"success": True}
        return 404, {"code": "not.found", "message": "Not found"}


class FakeDeepLinkServer:
    """``GET /<bot>?start=<payload>`` -> a small page naming the bot. Records hits."""

    def __init__(self):
        self.hits: list[str] = []
        self._server = None

    def start(self, port: int = 0) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                owner.hits.append(self.path)
                raw = (
                    "<!doctype html><title>MAX</title><h1 data-fake-max>MAX</h1>"
                    f"<p data-path>{urlsplit(self.path).path}</p>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        host, bound = self._server.server_address[:2]
        return f"http://{host}:{bound}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def main() -> None:  # pragma: no cover - manual browser acceptance helper
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=18780)
    parser.add_argument("--deep-link-port", type=int, default=18781)
    args = parser.parse_args()
    api = FakeMaxServer()
    link = FakeDeepLinkServer()
    print("deep link origin:", link.start(args.deep_link_port), flush=True)
    print("api:", api.start(args.api_port), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        link.stop()
        api.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
