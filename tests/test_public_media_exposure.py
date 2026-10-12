"""Private messenger attachments must not be reachable under public /media/.

Text-level pins of the proxy configuration (no Docker, no network). The live
production Caddyfile is not in the repository; ``Caddyfile.production.pre-max``
is its historical byte copy and documents the exposure that the reconcile
command and the deny rule close.
"""

import re
from pathlib import Path

import pytest
from django.test import override_settings
from django.urls import Resolver404, resolve

from apps.customer_requests.storage import PrivateAttachmentStorage

ROOT = Path(__file__).resolve().parents[1]
SERVING = [
    ROOT / "docker/caddy/Caddyfile",
    ROOT / "docker/caddy/Caddyfile.emergency",
    ROOT / "deploy/caddy/Caddyfile.production",
]
DENY = re.compile(
    r"@(?P<name>[a-z_]+) path /media/customer_requests/\*\n"
    r"\s*handle @(?P=name) \{\n\s*respond 404\n\s*\}"
)


def _site_blocks_serving_media(text):
    blocks = re.findall(r"^(\S[^\n]*\{\n.*?^\})", text, re.MULTILINE | re.DOTALL)
    return [block for block in blocks if "handle_path /media/*" in block]


@pytest.mark.parametrize("path", SERVING, ids=lambda p: p.name)
def test_every_site_that_serves_media_denies_customer_attachments(path):
    blocks = _site_blocks_serving_media(path.read_text(encoding="utf-8"))
    assert blocks, path
    for block in blocks:
        assert DENY.search(block), block
        # No directory listing anywhere under /media.
        assert "browse" not in block


def test_historical_live_copy_has_no_deny_rule():
    """The byte copy of the live file (2026-09-17) predates the deny rule.

    The rule reached the repository with dacb043 (2026-09-20), after the MAX
    edge install; the live file must be updated separately by the owner.
    """
    pre_max = (ROOT / "deploy/caddy/Caddyfile.production.pre-max").read_text(encoding="utf-8")
    assert "handle_path /media/*" in pre_max
    assert not DENY.search(pre_max)


def test_proxy_mounts_only_the_public_media_volume():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    proxy = compose.split("\n  proxy:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    assert "media:/srv/media:ro" in proxy
    assert "private_media" not in proxy


def test_django_never_serves_media_outside_debug():
    with pytest.raises(Resolver404):
        resolve("/media/customer_requests/sample.png")


def test_new_attachments_are_written_outside_public_media(tmp_path):
    public = tmp_path / "media"
    private = tmp_path / "private_media"
    with override_settings(MEDIA_ROOT=public, PRIVATE_MEDIA_ROOT=private):
        storage = PrivateAttachmentStorage()
    from django.core.files.base import ContentFile

    name = storage.save("customer_requests/sample.pdf", ContentFile(b"%PDF-1"))

    assert (private / name).is_file()
    assert not (public / name).exists()
    with pytest.raises(ValueError):
        storage.url(name)
