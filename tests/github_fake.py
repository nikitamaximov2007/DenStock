"""An isolated fake of the GitHub Releases API for the backup job's tests.

Only 127.0.0.1, no real GitHub, no real tokens. It mimics the behaviour the job
depends on:

* drafts are listed only to the write token; the read token cannot write;
* uploads go to a separate "upload host" prefix (``/uploads``) with the raw
  body, a duplicate asset name answers 422;
* an asset download answers 302 to ``/objects/<id>``, and that storage host
  REJECTS a request carrying ``Authorization`` (as S3-signed URLs do), so a
  client that forwards the token fails the tests;
* faults: ``outage`` (every call 503), ``cut_upload_after`` (the connection is
  dropped mid-upload, a ``starter`` asset stays behind), ``slow_upload``.

Every asset byte is counted; ``max_used`` records the peak, so a test can prove
the budget was never exceeded at ANY moment.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WRITE_TOKEN = "fake-write-token-0123456789"  # noqa: S105 - synthetic, never a real token
READ_TOKEN = "fake-read-token-9876543210"  # noqa: S105


class FakeGitHub:
    def __init__(self, blob_dir: Path, *, repo="owner/denstock-backups", private=True):
        self.repo = repo
        self.private = private
        self.blob_dir = blob_dir
        blob_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.releases: dict[int, dict] = {}
        self.assets: dict[int, dict] = {}
        self.next_id = 1000
        self.faults: dict = {}
        self.max_used = 0
        self.auth_leaks = 0
        self.calls: list[tuple[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # --- lifecycle ------------------------------------------------------------
    @property
    def api(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def uploads(self):
        return f"http://127.0.0.1:{self.port}/uploads"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # --- state helpers for tests -----------------------------------------------
    def used(self):
        return sum(a["size"] for a in self.assets.values())

    def _note_usage(self):
        self.max_used = max(self.max_used, self.used())

    def blob(self, asset_id) -> Path:
        return self.blob_dir / str(asset_id)

    def published(self):
        return sorted((r for r in self.releases.values() if not r["draft"]),
                      key=lambda r: r["tag_name"])

    def add_release(self, tag, *, name=None, body="", draft=False, assets=()):
        """Seed a release directly (e.g. an unrecognized one or a manual [keep])."""
        with self.lock:
            rid = self._new_id()
            self.releases[rid] = {"id": rid, "tag_name": tag, "name": name or tag, "body": body,
                                  "draft": draft, "asset_ids": []}
            for asset_name, data in assets:
                self._store_asset(rid, asset_name, data, "uploaded")
            self._note_usage()
            return rid

    def corrupt(self, asset_id):
        """Flip one byte in the middle: same size, different SHA-256."""
        path = self.blob(asset_id)
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))

    def asset_by_name(self, name):
        return next(a for a in self.assets.values() if a["name"] == name)

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def _store_asset(self, rid, name, data, state):
        aid = self._new_id()
        self.blob(aid).write_bytes(data)
        self.assets[aid] = {"id": aid, "name": name, "size": len(data), "state": state,
                            "release_id": rid}
        self.releases[rid]["asset_ids"].append(aid)
        return aid

    def _release_json(self, rel):
        return {"id": rel["id"], "tag_name": rel["tag_name"], "name": rel["name"],
                "body": rel["body"], "draft": rel["draft"],
                "upload_url": (f"{self.uploads}/repos/{self.repo}/releases/{rel['id']}"
                               "/assets{?name,label}"),
                # The embedded copy is deliberately truncated: the job must use the
                # paginated assets endpoint for accounting.
                "assets": [self._asset_json(self.assets[a]) for a in rel["asset_ids"]][:1]}

    @staticmethod
    def _asset_json(asset):
        return {"id": asset["id"], "name": asset["name"], "size": asset["size"],
                "state": asset["state"]}

    # --- HTTP -----------------------------------------------------------------
    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _send(self, code, payload=None, headers=None):
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _token(self):
                header = self.headers.get("Authorization", "")
                return header[7:] if header.startswith("Bearer ") else None

            def _drain(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def handle_one(self, method):
                url = urlparse(self.path)
                fake.calls.append((method, url.path))
                if fake.faults.get("outage"):
                    self._drain()
                    return self._send(503, {"message": "Service Unavailable"})
                if method == "GET" and fake.faults.get("rate_limit_gets", 0) > 0:
                    fake.faults["rate_limit_gets"] -= 1
                    return self._send(429, {"message": "rate limited"}, {"Retry-After": "0"})
                if method == "DELETE" and fake.faults.get("delete_fail"):
                    self._drain()
                    return self._send(503, {"message": "Service Unavailable"})
                if fake.faults.get("bad_json") and url.path.endswith("/releases"):
                    fake.faults.pop("bad_json")
                    body = b"<html>oops</html>"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return None
                if url.path.startswith("/objects/"):
                    return self._object(url)
                token = self._token()
                if token not in (WRITE_TOKEN, READ_TOKEN):
                    self._drain()
                    return self._send(401, {"message": "Bad credentials"})
                writer = token == WRITE_TOKEN
                base = f"/repos/{fake.repo}"
                upload_base = f"/uploads{base}/releases/"
                if url.path.startswith(upload_base):
                    return self._upload(url, writer)
                if url.path == base and method == "GET":
                    return self._send(200, {"full_name": fake.repo, "private": fake.private})
                if url.path == f"{base}/releases" and method == "GET":
                    return self._list(url, writer)
                if url.path == f"{base}/releases" and method == "POST":
                    return self._create(writer)
                match = re.fullmatch(rf"{re.escape(base)}/releases/(\d+)", url.path)
                if match and method == "PATCH":
                    return self._patch(int(match.group(1)), writer)
                if match and method == "DELETE":
                    return self._delete(int(match.group(1)), writer)
                match = re.fullmatch(rf"{re.escape(base)}/releases/(\d+)/assets", url.path)
                if match and method == "GET":
                    return self._assets(url, int(match.group(1)))
                match = re.fullmatch(rf"{re.escape(base)}/releases/assets/(\d+)", url.path)
                if match and method == "GET":
                    return self._asset(int(match.group(1)))
                self._drain()
                return self._send(404, {"message": "Not Found"})

            def _list(self, url, writer):
                query = parse_qs(url.query)
                per_page = int(query.get("per_page", ["30"])[0])
                page = int(query.get("page", ["1"])[0])
                with fake.lock:
                    rels = sorted(fake.releases.values(), key=lambda r: -r["id"])
                    rels = [r for r in rels if writer or not r["draft"]]
                    chunk = rels[(page - 1) * per_page: page * per_page]
                    payload = [fake._release_json(r) for r in chunk]
                return self._send(200, payload)

            def _assets(self, url, rid):
                query = parse_qs(url.query)
                per_page = int(query.get("per_page", ["30"])[0])
                page = int(query.get("page", ["1"])[0])
                with fake.lock:
                    rel = fake.releases.get(rid)
                    if rel is None:
                        return self._send(404, {"message": "Not Found"})
                    items = [fake._asset_json(fake.assets[a]) for a in rel["asset_ids"]]
                    if fake.faults.get("size_lie"):
                        for item in items:
                            item["size"] += fake.faults["size_lie"]
                return self._send(200, items[(page - 1) * per_page: page * per_page])

            def _create(self, writer):
                data = json.loads(self._drain() or b"{}")
                if not writer:
                    return self._send(403, {"message": "Resource not accessible"})
                with fake.lock:
                    if any(r["tag_name"] == data["tag_name"] and not r["draft"]
                           for r in fake.releases.values()):
                        return self._send(422, {"message": "already_exists"})
                    rid = fake._new_id()
                    fake.releases[rid] = {"id": rid, "tag_name": data["tag_name"],
                                          "name": data.get("name", data["tag_name"]),
                                          "body": data.get("body", ""),
                                          "draft": bool(data.get("draft")), "asset_ids": []}
                    return self._send(201, fake._release_json(fake.releases[rid]))

            def _patch(self, rid, writer):
                data = json.loads(self._drain() or b"{}")
                if not writer:
                    return self._send(403, {"message": "Resource not accessible"})
                with fake.lock:
                    rel = fake.releases.get(rid)
                    if rel is None:
                        return self._send(404, {"message": "Not Found"})
                    if "draft" in data:
                        rel["draft"] = bool(data["draft"])
                    return self._send(200, fake._release_json(rel))

            def _delete(self, rid, writer):
                self._drain()
                if not writer:
                    return self._send(403, {"message": "Resource not accessible"})
                with fake.lock:
                    rel = fake.releases.pop(rid, None)
                    if rel is None:
                        return self._send(404, {"message": "Not Found"})
                    for aid in rel["asset_ids"]:
                        fake.assets.pop(aid, None)
                        fake.blob(aid).unlink(missing_ok=True)
                return self._send(204)

            def _upload(self, url, writer):
                match = re.fullmatch(r"/uploads/repos/[^/]+/[^/]+/releases/(\d+)/assets", url.path)
                name = parse_qs(url.query).get("name", [""])[0]
                length = int(self.headers.get("Content-Length") or 0)
                if not writer or not match or not name:
                    self.rfile.read(length)
                    return self._send(403 if not writer else 400, {"message": "bad upload"})
                rid = int(match.group(1))
                with fake.lock:
                    rel = fake.releases.get(rid)
                    if rel is None or any(fake.assets[a]["name"] == name for a in rel["asset_ids"]):
                        self.rfile.read(length)
                        return self._send(422 if rel else 404, {"message": "already_exists"})
                cut = fake.faults.get("cut_upload_after")
                if cut is not None and length > cut:
                    fake.rfile_partial = self.rfile.read(cut)
                    with fake.lock:
                        fake._store_asset(rid, name, b"", "starter")
                    fake.faults.pop("cut_upload_after", None)
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return None
                received = bytearray()
                while len(received) < length:
                    chunk = self.rfile.read(min(1 << 20, length - len(received)))
                    if not chunk:
                        return None
                    received.extend(chunk)
                    if fake.faults.get("slow_upload"):
                        time.sleep(fake.faults["slow_upload"])
                with fake.lock:
                    aid = fake._store_asset(rid, name, bytes(received), "uploaded")
                    fake._note_usage()
                    return self._send(201, fake._asset_json(fake.assets[aid]))

            def _asset(self, aid):
                if "octet-stream" not in self.headers.get("Accept", ""):
                    with fake.lock:
                        asset = fake.assets.get(aid)
                    if asset is None:
                        return self._send(404, {"message": "Not Found"})
                    return self._send(200, fake._asset_json(asset))
                if aid not in fake.assets:
                    return self._send(404, {"message": "Not Found"})
                return self._send(302, None, {"Location": f"{fake.api}/objects/{aid}?sig=x"})

            def _object(self, url):
                if self.headers.get("Authorization"):
                    fake.auth_leaks += 1
                    return self._send(400, {"message": "Only one auth mechanism allowed"})
                aid = int(url.path.rsplit("/", 1)[1])
                path = fake.blob(aid)
                if aid not in fake.assets or not path.exists():
                    return self._send(404, {"message": "NoSuchKey"})
                size = path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with path.open("rb") as fh:
                    while chunk := fh.read(1 << 20):
                        self.wfile.write(chunk)
                return None

            def do_GET(self):  # noqa: N802
                self.handle_one("GET")

            def do_POST(self):  # noqa: N802
                self.handle_one("POST")

            def do_PATCH(self):  # noqa: N802
                self.handle_one("PATCH")

            def do_DELETE(self):  # noqa: N802
                self.handle_one("DELETE")

        return Handler


if __name__ == "__main__":  # used by scripts/dr/synthetic_drill.sh GITHUB=1
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", required=True, help="blob directory")
    parser.add_argument("--port-file", required=True)
    args = parser.parse_args()
    server = FakeGitHub(Path(args.serve))
    Path(args.port_file).write_text(str(server.port))
    server.server.serve_forever()
