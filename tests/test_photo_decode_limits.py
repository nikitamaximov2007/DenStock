"""The photo decoder must not multiply its worst-case WEBP peak across workers."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.test import override_settings
from PIL import Image

from apps.core.photo_decode import photo_decode_slot
from apps.customer_requests.attachments import ValidatedAttachment, validate_photo_attachment


@pytest.mark.django_db(transaction=True)
@override_settings(DENSTOCK_MODE="development")
def test_photo_decode_is_serialized_across_workers(monkeypatch):
    active = 0
    peak = 0
    guard = threading.Lock()
    start = threading.Barrier(4)

    class FakeImage:
        width = 1
        height = 1
        format = "PNG"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def load(self):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with guard:
                active -= 1

    monkeypatch.setattr(Image, "open", lambda _source: FakeImage())
    monkeypatch.setitem(Image.MIME, "PNG", "image/png")
    attachment = ValidatedAttachment(b"synthetic", "photo.png", "image/png")

    def decode(_index):
        start.wait(timeout=10)
        assert validate_photo_attachment(attachment) is attachment

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(decode, range(4)))
    assert peak == 1
    assert active == 0


@pytest.mark.django_db(transaction=True)
@override_settings(DENSTOCK_MODE="development")
def test_catalog_decode_excludes_other_decodes():
    active = []
    violations = []
    guard = threading.Lock()
    start = threading.Barrier(4)

    def decode(catalog):
        start.wait(timeout=10)
        with photo_decode_slot():
            with guard:
                active.append(catalog)
                if len(active) != 1:
                    violations.append(tuple(active))
            time.sleep(0.05)
            with guard:
                active.remove(catalog)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(decode, (True, False, True, False)))
    assert not violations
    assert not active
