"""The private-attachment guard, proven against a REAL Caddy 2 (Docker).

The static test pins the text of the Caddyfiles; this one runs the tracked
``deploy/caddy/Caddyfile.production`` in ``caddy:2`` on an internal Docker
network with a stub ``web``/``catalog-web`` upstream and a synthetic media
volume, and sends real HTTP requests: plain, upper-case, percent-encoded,
dot-segment and double-slash variants must never return a private file, while
public media and Django routes such as ``/admin/customer_requests/`` keep
working. Only TLS is switched off (``auto_https off``) so the named hosts
answer on port 80 without contacting any certificate authority.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "deploy/caddy/Caddyfile.production"
SECRET = b"PRIVATE-ATTACHMENT-CONTENT"
PUBLIC = b"PUBLIC-PART-PHOTO"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(["docker", "image", "inspect", "caddy:2"], capture_output=True)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="Docker with caddy:2 required")


def _caddyfile_for_test(*, guard: bool) -> str:
    """The tracked file, with TLS off and named hosts on plain HTTP so that the
    Host-routed blocks answer on port 80. Nothing else is changed."""
    text = PRODUCTION.read_text()
    for host in ("pro-brp.ru {", "www.pro-brp.ru {", "catalog.185-250-44-206.sslip.io {"):
        text = text.replace(f"\n{host}", f"\nhttp://{host}")
    if not guard:
        text = "\n".join(line for line in text.splitlines() if "private_attachments" not in line)
    return "{\n\tauto_https off\n\tadmin off\n}\n" + text


@pytest.fixture(scope="module")
def edge(tmp_path_factory):
    yield from _edge(tmp_path_factory, guard=True)


@pytest.fixture(scope="module")
def unguarded_edge(tmp_path_factory):
    yield from _edge(tmp_path_factory, guard=False)


def _edge(tmp_path_factory, *, guard):
    tmp = tmp_path_factory.mktemp("caddy-guard")
    media = tmp / "media"
    for rel in ("customer_requests/1/scan.pdf", "CUSTOMER_REQUESTS/1/scan.pdf",
                "Customer_Requests/1/scan.pdf", "sub/customer_requests/1/scan.pdf"):
        (media / rel).parent.mkdir(parents=True, exist_ok=True)
        (media / rel).write_bytes(SECRET)
    (media / "part-items").mkdir(parents=True)
    (media / "part-items/photo.jpg").write_bytes(PUBLIC)
    (media / "customer_requests_public_note.txt").write_bytes(PUBLIC)
    media.chmod(0o755)
    for path in media.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    caddyfile = tmp / "Caddyfile"
    caddyfile.write_text(_caddyfile_for_test(guard=guard))
    stub = tmp / "Stubfile"
    stub.write_text(':8000 {\n\trespond "{system.hostname} {http.request.uri.path}" 200\n}\n')
    name = "cg-" + secrets.token_hex(4)
    net = name + "-net"
    run = lambda *a: subprocess.run(["docker", *a], check=True, capture_output=True, text=True)  # noqa: E731
    run("network", "create", net)
    try:
        for alias in ("web", "catalog-web"):
            run("run", "-d", "--name", f"{name}-{alias}", "--hostname", alias, "--network", net,
                "--network-alias", alias, "-v", f"{stub}:/etc/caddy/Caddyfile:ro", "caddy:2")
        run("run", "-d", "--name", f"{name}-proxy", "--network", net,
            "-e", "CADDY_SITE_ADDRESS=:80", "-p", "127.0.0.1::80",
            "-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro", "-v", f"{media}:/srv/media:ro",
            "caddy:2")
        port = json.loads(run("inspect", f"{name}-proxy").stdout)[0]["NetworkSettings"][
            "Ports"]["80/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            probe = ["curl", "-s", "-o", "/dev/null", base + "/media/part-items/photo.jpg"]
            if subprocess.run(probe, capture_output=True).returncode == 0:
                break
            time.sleep(0.25)
        yield base
    finally:
        for suffix in ("proxy", "web", "catalog-web"):
            subprocess.run(["docker", "rm", "-f", f"{name}-{suffix}"], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


def _get(base, raw_path, host=None):
    # --path-as-is: curl must not normalise the path; the edge has to.
    cmd = ["curl", "-s", "--path-as-is", "-o", "-", "-w", "\n%{http_code}", base + raw_path]
    if host:
        cmd += ["-H", f"Host: {host}"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    body, _, code = out.rpartition(b"\n")
    return int(code), body


PRIVATE_VARIANTS = [
    "/media/customer_requests/1/scan.pdf",
    "/media/CUSTOMER_REQUESTS/1/scan.pdf",
    "/media/Customer_Requests/1/scan.pdf",
    "/media/customer_requests%2F1%2Fscan.pdf",
    "/media/customer_requests%2f1/scan.pdf",
    "/media/customer%5Frequests/1/scan.pdf",
    "/media/%63ustomer_requests/1/scan.pdf",
    "/media//customer_requests/1/scan.pdf",
    "/media/./customer_requests/1/scan.pdf",
    "/media/part-items/../customer_requests/1/scan.pdf",
    "/media/part-items/%2e%2e/customer_requests/1/scan.pdf",
    "/media/sub/customer_requests/1/scan.pdf",
    "/media/customer_requests/",
    "/media/customer_requests",
    "/MEDIA/customer_requests/1/scan.pdf",
]


@pytest.mark.parametrize("path", PRIVATE_VARIANTS)
def test_no_variant_returns_a_private_attachment(edge, path):
    code, body = _get(edge, path)
    assert SECRET not in body, (path, code)
    assert code in (404, 400) or body.startswith(b"web "), (path, code, body[:80])


def test_public_media_still_served(edge):
    assert _get(edge, "/media/part-items/photo.jpg") == (200, PUBLIC)
    # a name that merely starts with the word is not a private directory
    assert _get(edge, "/media/customer_requests_public_note.txt") == (200, PUBLIC)


@pytest.mark.parametrize("path", [
    "/admin/customer_requests/", "/admin/customer_requests/customerrequest/",
    "/customer-requests/", "/sales/customers/"])
def test_django_routes_with_the_word_still_reach_web(edge, path):
    code, body = _get(edge, path)
    assert code == 200 and body == f"web {path}".encode(), (path, code, body)


def test_public_catalog_host_never_reaches_media_or_web(edge):
    code, body = _get(edge, "/media/customer_requests/1/scan.pdf", host="pro-brp.ru")
    assert SECRET not in body and body.startswith(b"catalog-web "), (code, body)
    code, body = _get(edge, "/customer-requests/max/webhook/", host="pro-brp.ru")
    assert body.startswith(b"catalog-web ")  # GET is not the MAX webhook route


def test_negative_control_without_the_guard_the_file_leaks(unguarded_edge):
    """Proves the harness can see a leak: the same setup minus the guard line
    serves the private file, so the passes above are not vacuous."""
    leaking = [p for p in PRIVATE_VARIANTS if not p.endswith(("customer_requests/",
                                                              "customer_requests"))]
    for path in leaking:  # every file variant really leaks without the guard
        assert _get(unguarded_edge, path) == (200, SECRET), path


def _head(base, raw_path):
    out = subprocess.run(["curl", "-s", "--path-as-is", "-I", "-o", "/dev/null", "-w",
                          "%{http_code} %{size_download}", base + raw_path],
                         capture_output=True, check=True, text=True).stdout
    code, size = out.split()
    return int(code), int(size)


@pytest.mark.parametrize("path", PRIVATE_VARIANTS)
def test_head_requests_never_confirm_a_private_file(edge, path):
    code, _ = _head(edge, path)
    assert code in (404, 400), (path, code)


def test_head_on_public_media_still_works(edge):
    assert _head(edge, "/media/part-items/photo.jpg")[0] == 200
