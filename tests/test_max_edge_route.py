"""Production edge route for MAX's webhook: one path, one method, one host.

``deploy/caddy/Caddyfile.production.pre-max`` is a byte copy of the Caddyfile
production serves today (read-only preflight, sha256 below). The candidate
``Caddyfile.production`` must differ from it only inside the pro-brp.ru block,
by the MAX webhook route. ``scripts/qualification/max_edge_check.py`` proves
the same with real Caddy; these tests pin the text without Docker.
"""

import difflib
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CADDY = ROOT / "deploy" / "caddy"
LIVE_SHA256 = "ff363d12176b1da8a939f06fca9d8198cb614dc87d592b58368a5cc38d0f0340"
PRE = (CADDY / "Caddyfile.production.pre-max").read_text(encoding="utf-8")
POST = (CADDY / "Caddyfile.production").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "scripts" / "qualification"))

import max_edge_check  # noqa: E402


def _block(text: str, site: str) -> str:
    match = re.search(rf"^{re.escape(site)} \{{\n(.*?)^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, site
    return match.group(1)


def test_pre_max_copy_is_exactly_what_production_serves():
    assert hashlib.sha256(PRE.encode("utf-8")).hexdigest() == LIVE_SHA256


def test_only_the_public_catalog_block_changes():
    for site in ("{$CADDY_SITE_ADDRESS::80}", "catalog.185-250-44-206.sslip.io", "www.pro-brp.ru"):
        assert _block(PRE, site) == _block(POST, site), site
    outside_pre = re.sub(r"^pro-brp\.ru \{\n.*?^\}", "", PRE, flags=re.MULTILINE | re.DOTALL)
    outside_post = re.sub(r"^pro-brp\.ru \{\n.*?^\}", "", POST, flags=re.MULTILINE | re.DOTALL)
    changed = [
        line for line in difflib.unified_diff(
            outside_pre.splitlines(), outside_post.splitlines(), lineterm="", n=0
        ) if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert all(line[1:].startswith("#") for line in changed), changed


def test_the_webhook_route_is_exact_post_only_to_web():
    block = _block(POST, "pro-brp.ru")
    matcher = re.search(r"@max_webhook \{\n(.*?)\n\t\}", block, re.DOTALL).group(1)
    assert [line.strip() for line in matcher.splitlines()] == [
        "method POST",
        "path /customer-requests/max/webhook/",
    ]
    assert "*" not in matcher
    assert block.count("reverse_proxy web:8000") == 1
    handler = block.split("handle @max_webhook {", 1)[1].split("\n\t}\n", 1)[0]
    assert "reverse_proxy web:8000" in handler
    assert "header_up Host 185-250-44-206.sslip.io" in handler
    assert "max_size 64KB" in handler
    # Everything else on the public host still reaches only catalog-web.
    assert block.rstrip().endswith("handle {\n\t\treverse_proxy catalog-web:8000\n\t}")


def test_no_broad_customer_requests_route_and_no_other_host_reaches_web():
    directives = [
        line.strip()
        for line in POST.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    paths = [line for line in directives if line.startswith(("path ", "handle_path ", "@"))]
    customer_paths = [line for line in paths if "customer-requests" in line]
    assert customer_paths == ["path /customer-requests/max/webhook/"]
    assert "reverse_proxy web:8000" not in _block(POST, "catalog.185-250-44-206.sslip.io")
    assert "web:8000" not in _block(POST, "www.pro-brp.ru")


def test_the_rewritten_host_is_one_web_already_allows():
    # Production's DJANGO_ALLOWED_HOSTS (read-only preflight) contains it; the
    # public catalog domain is deliberately not an internal allowed host.
    assert max_edge_check.INTERNAL == "185-250-44-206.sslip.io"


def test_the_public_urlconf_still_has_no_webhook():
    from django.urls import Resolver404, get_resolver

    resolver = get_resolver("config.public_urls")
    try:
        resolver.resolve("/customer-requests/max/webhook/")
    except Resolver404:
        return
    raise AssertionError("catalog-web must not route the MAX webhook")


def test_the_checker_only_switches_the_test_copy_to_plain_http():
    copy = max_edge_check.plain_http_copy(POST)
    assert copy.startswith("{\n\tauto_https off\n}\n")
    assert "http://pro-brp.ru {" in copy and "http://www.pro-brp.ru {" in copy
    assert copy.count("@max_webhook") == POST.count("@max_webhook")
    assert (CADDY / "Caddyfile.production").read_text(encoding="utf-8") == POST
