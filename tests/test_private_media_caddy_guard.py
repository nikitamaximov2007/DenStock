import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVING = (
    ROOT / "docker/caddy/Caddyfile",
    ROOT / "docker/caddy/Caddyfile.emergency",
    ROOT / "deploy/caddy/Caddyfile.production",
)
GUARD = re.compile(
    r"@private_customer_media path_regexp private_customer_media "
    r"\(\?i\)\(\^\|/\|%2f\)customer_requests\(/\|%2f\|\$\)"
)


@pytest.mark.parametrize("path", SERVING, ids=lambda item: item.name)
def test_every_public_media_server_has_the_case_and_encoding_safe_guard(path):
    text = path.read_text(encoding="utf-8")
    assert "handle_path /media/*" in text
    assert GUARD.search(text), path
    assert "respond 404" in text or "respond @private_customer_media 404" in text
    assert "browse" not in text


def test_guard_expression_covers_case_and_encoded_path_separators():
    expression = re.compile(r"(^|/|%2f)customer_requests(/|%2f|$)", re.IGNORECASE)
    for path in (
        "/media/customer_requests/file.png",
        "/MEDIA/CUSTOMER_REQUESTS/file.png",
        "/media%2fcustomer_requests%2ffile.png",
    ):
        assert expression.search(path)
    assert not expression.search("/media/customer_requests-old/file.png")
