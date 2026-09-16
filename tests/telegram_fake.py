"""A local Telegram Bot API stand-in for release rehearsals. No network, no real token.

Speaks what telegram-bot calls (getMe, getWebhookInfo, getUpdates, sendMessage,
answerCallbackQuery) and adds two test endpoints: ``POST /_fake/update`` queues
an update for the bot's long poll, ``GET /_fake/sent`` lists what it sent.

    python -m tests.telegram_fake --port 18790 --bind 0.0.0.0
"""
from __future__ import annotations

import argparse
import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeTelegram:
    def __init__(self):
        self.lock = threading.Condition()
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.answers: list[dict] = []
        self.ids = itertools.count(1)
        self.message_ids = itertools.count(1000)

    def method(self, name: str, body: dict):
        if name == "getMe":
            return {"id": 777000, "is_bot": True, "username": "sim_telegram_bot"}
        if name == "getWebhookInfo":
            return {"url": ""}
        if name == "getUpdates":
            offset = int(body.get("offset") or 0)
            deadline = time.monotonic() + min(float(body.get("timeout") or 0), 2.0)
            with self.lock:
                while True:
                    due = [u for u in self.updates if u["update_id"] >= offset]
                    if due or time.monotonic() >= deadline:
                        return due
                    self.lock.wait(max(deadline - time.monotonic(), 0))
        if name == "sendMessage":
            with self.lock:
                message_id = next(self.message_ids)
                self.sent.append({"chat_id": body.get("chat_id"), "text": body.get("text"),
                                  "reply_markup": body.get("reply_markup"),
                                  "message_id": message_id})
            return {"message_id": message_id, "chat": {"id": body.get("chat_id")}}
        if name == "answerCallbackQuery":
            with self.lock:
                self.answers.append(body)
            return True
        return None

    def enqueue(self, update: dict) -> dict:
        with self.lock:
            update = dict(update, update_id=next(self.ids))
            self.updates.append(update)
            self.lock.notify_all()
        return update


def serve(port: int, bind: str) -> None:  # pragma: no cover - rehearsal helper
    fake = FakeTelegram()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _reply(self, payload, code=200):
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/_fake/sent":
                with fake.lock:
                    return self._reply({"sent": fake.sent, "answers": fake.answers})
            return self._reply({"ok": False}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/_fake/update":
                return self._reply(fake.enqueue(body))
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or not parts[0].startswith("bot"):
                return self._reply({"ok": False, "error_code": 404, "description": "Not Found"},
                                   404)
            result = fake.method(parts[1], body)
            if result is None:
                return self._reply({"ok": False, "error_code": 400, "description": "Bad Request"},
                                   400)
            return self._reply({"ok": True, "result": result})

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    print(f"fake telegram on {bind}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18790)
    parser.add_argument("--bind", default="127.0.0.1")
    arguments = parser.parse_args()
    serve(arguments.port, arguments.bind)
