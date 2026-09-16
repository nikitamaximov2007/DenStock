#!/usr/bin/env python3
"""Prove what the production Caddyfile sends to internal web, with real Caddy.

Runs the given Caddyfile in a throwaway caddy:2 container in front of two echo
backends named ``web`` and ``catalog-web``, then sends requests with the real
production host names in the Host header. HTTPS is switched off only in this
test copy (a global ``auto_https off`` block is prepended), so no certificate is
requested from anyone; routing is exactly what production parses.

    python3 scripts/qualification/max_edge_check.py deploy/caddy/Caddyfile.production
    python3 scripts/qualification/max_edge_check.py --expect pre-max \\
        deploy/caddy/Caddyfile.production.pre-max

Needs only Docker and the caddy:2 image. Never touches a real server.
Exit code 0 means every expectation held.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PUBLIC = "pro-brp.ru"
# web's own site name: what production sets as CADDY_SITE_ADDRESS and what the
# MAX route rewrites the Host header to.
INTERNAL = "185-250-44-206.sslip.io"
PREVIEW = "catalog.185-250-44-206.sslip.io"
WEBHOOK = "/customer-requests/max/webhook/"
ECHO = """:8000 {
\theader Content-Type application/json
\trespond `{"backend": "%s", "method": "{method}", "uri": "{uri}", "host": "{host}"}` 200
}
"""


SITE_LINE = re.compile(r"^([A-Za-z0-9.-]+\.[A-Za-z]{2,}) \{$", re.MULTILINE)


def plain_http_copy(text: str) -> str:
    """The same routing on plain HTTP: no certificates, no ACME, same matchers.

    Caddy serves a bare host name on port 443 even with automatic HTTPS off, so
    each named site gets an explicit ``http://``; nothing else changes.
    """
    return "{\n\tauto_https off\n}\n\n" + SITE_LINE.sub(r"http://\1 {", text)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port, host, method, path, body=b""):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body or None, method=method,
        headers={"Host": host, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw.decode() or "{}")
    except ValueError:
        return status, {}


def expectations(mode: str):
    webhook_backend = "web" if mode == "max" else "catalog-web"
    big = b"x" * (70 * 1024)
    return [
        ("POST webhook on public host", PUBLIC, "POST", WEBHOOK, b"{}", 200, webhook_backend),
        ("GET webhook on public host", PUBLIC, "GET", WEBHOOK, b"", 200, "catalog-web"),
        ("webhook without slash", PUBLIC, "POST", WEBHOOK.rstrip("/"), b"{}", 200,
         "catalog-web"),
        ("webhook subpath", PUBLIC, "POST", WEBHOOK + "x/", b"{}", 200, "catalog-web"),
        ("webhook with query", PUBLIC, "POST", WEBHOOK + "?a=1", b"{}", 200, webhook_backend),
        ("telegram webhook", PUBLIC, "POST", "/customer-requests/telegram/webhook/", b"{}",
         200, "catalog-web"),
        ("customer requests list", PUBLIC, "GET", "/customer-requests/", b"", 200,
         "catalog-web"),
        ("admin", PUBLIC, "GET", "/admin/", b"", 200, "catalog-web"),
        ("login", PUBLIC, "POST", "/login/", b"x", 200, "catalog-web"),
        ("catalog root", PUBLIC, "GET", "/", b"", 200, "catalog-web"),
        ("request submit", PUBLIC, "POST", "/request/submit/", b"x", 200, "catalog-web"),
        ("max handoff", PUBLIC, "POST", "/request/success/0/max/", b"", 200, "catalog-web"),
        ("preview host", PREVIEW, "POST", WEBHOOK, b"{}", 200, "catalog-web"),
        ("internal host unchanged", INTERNAL, "GET", "/customer-requests/", b"", 200, "web"),
        ("oversized webhook body", PUBLIC, "POST", WEBHOOK, big, 413 if mode == "max" else 200,
         None if mode == "max" else "catalog-web"),
    ]


def run(caddyfile: Path, mode: str) -> int:
    tag = uuid.uuid4().hex[:8]
    network = f"max-edge-{tag}"
    names = []
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        test_copy = tmp_path / "Caddyfile"
        test_copy.write_text(plain_http_copy(caddyfile.read_text()))
        port = _free_port()
        try:
            _docker("network", "create", network)
            for backend in ("web", "catalog-web"):
                echo = tmp_path / f"{backend}.Caddyfile"
                echo.write_text(ECHO % backend)
                name = f"{network}-{backend}"
                _docker("run", "-d", "--name", name, "--network", network,
                        "--network-alias", backend, "-v", f"{echo}:/etc/caddy/Caddyfile:ro",
                        "caddy:2")
                names.append(name)
            proxy = f"{network}-proxy"
            _docker("run", "-d", "--name", proxy, "--network", network,
                    "-e", f"CADDY_SITE_ADDRESS=http://{INTERNAL}",
                    "-p", f"127.0.0.1:{port}:80", "-v", f"{test_copy}:/etc/caddy/Caddyfile:ro",
                    "caddy:2")
            names.append(proxy)
            for _ in range(50):
                try:
                    _request(port, PUBLIC, "GET", "/")
                    break
                except OSError:
                    time.sleep(0.2)
            for label, host, method, path, body, status, backend in expectations(mode):
                got_status, got = _request(port, host, method, path, body)
                ok = got_status == status and (backend is None or got.get("backend") == backend)
                if ok and backend == "web" and host == PUBLIC:
                    ok = got.get("host") == INTERNAL  # Host rewritten to web's own site
                failures += not ok
                print(f"{'PASS' if ok else 'FAIL'}  {label}: {method} {host}{path} -> "
                      f"{got_status} {got.get('backend', '-')} host={got.get('host', '-')}")
            code, www = _request(port, "www." + PUBLIC, "GET", "/x")
            ok = code in (301, 308)
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  www redirect: {code}")
        finally:
            for name in names:
                _docker("rm", "-f", name, check=False)
            _docker("network", "rm", network, check=False)
    print(json.dumps({"caddyfile": str(caddyfile), "mode": mode, "failures": failures}))
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("caddyfile", type=Path)
    parser.add_argument("--expect", choices=("max", "pre-max"), default="max")
    args = parser.parse_args(argv)
    return run(args.caddyfile, args.expect)


if __name__ == "__main__":
    sys.exit(main())
