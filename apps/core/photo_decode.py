"""Bound decoded-image memory across the web and bot processes."""

import threading
from contextlib import contextmanager

from django.conf import settings
from django.db import connection

# A 16 MP WEBP can consume hundreds of MiB while decoding. The one-vCPU,
# 2 GiB deployment gets one decoder across web and both bot containers.
PHOTO_DECODE_LOCK_KEY = 0x44534B50484F5441
_LOCAL_SLOT = threading.Lock()


@contextmanager
def photo_decode_slot():
    if settings.DENSTOCK_MODE == "test" or connection.vendor != "postgresql":
        with _LOCAL_SLOT:
            yield
        return

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [PHOTO_DECODE_LOCK_KEY])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [PHOTO_DECODE_LOCK_KEY])
