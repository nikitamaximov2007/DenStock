"""Customer attachments are never served from public media by any edge config.

The emergency guard was applied directly to the live Caddyfile on 2026-10-12.
``deploy/caddy/Caddyfile.production`` is now its byte copy: a future install
from the repository cannot silently drop it, and any drift from the live file
must be a deliberate, reviewed change of the pinned hash below.

The guard lives INSIDE ``handle_path /media/*`` on purpose. A site-level
matcher on ``customer_requests`` would also return 404 for Django routes such
as ``/admin/customer_requests/`` (the app's admin), which must keep working.
"""

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "deploy/caddy/Caddyfile.production"
SERVING = (
    PRODUCTION,
    ROOT / "docker/caddy/Caddyfile",
    ROOT / "docker/caddy/Caddyfile.emergency",
)
# sha256 of /etc/denstock/caddy/Caddyfile read on production 2026-10-12.
LIVE_SHA256 = "8ba9fb9edf7cf96d6b28914286cf245c0b13be40c316a8047ca578ccb2d12346"
EXPRESSION = r"(?i)(^|/|%2f)customer_requests(/|%2f|$)"
GUARD_LINE = f"@private_attachments path_regexp private_attachments {EXPRESSION}"


def _media_block(text: str) -> str:
    match = re.search(r"handle_path /media/\* \{\n(.*?)\n\t\}", text, re.DOTALL)
    assert match, "handle_path /media/* block is missing"
    return match.group(1)


def test_tracked_production_caddyfile_is_the_live_file():
    assert hashlib.sha256(PRODUCTION.read_bytes()).hexdigest() == LIVE_SHA256


@pytest.mark.parametrize("path", SERVING, ids=lambda item: item.name)
def test_guard_is_inside_the_media_handler_before_file_server(path):
    text = path.read_text(encoding="utf-8")
    block = _media_block(text)
    assert GUARD_LINE in block
    assert "respond @private_attachments 404" in block
    assert block.index("respond @private_attachments 404") < block.index("file_server")
    assert "browse" not in block


@pytest.mark.parametrize("path", SERVING, ids=lambda item: item.name)
def test_no_site_level_matcher_can_shadow_django_routes(path):
    text = path.read_text(encoding="utf-8")
    outside = text.replace(_media_block(text), "")
    assert "customer_requests" not in outside


# Inside handle_path the matcher sees the path with /media stripped.
@pytest.mark.parametrize(
    "path",
    [
        "/customer_requests/scan.png",
        "/CUSTOMER_REQUESTS/scan.png",
        "/Customer_Requests/7/scan.png",
        "//customer_requests/scan.png",
        "/./customer_requests/scan.png",
        "/part-types/../customer_requests/scan.png",
        "/customer_requests%2Fscan.png",
        "%2fcustomer_requests%2fscan.png",
        "/customer_requests",
        "/customer_requests/",
    ],
)
def test_expression_blocks_every_variant_of_the_attachment_path(path):
    assert re.search(EXPRESSION, path)


@pytest.mark.parametrize(
    "path",
    ["/part-types/photo.jpg", "/part-items/1/photo.jpg", "/customer_requests-old/x.png"],
)
def test_expression_leaves_ordinary_public_media_alone(path):
    assert not re.search(EXPRESSION, path)
