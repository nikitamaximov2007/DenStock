"""Named crash points for the offline DR tools (tests only).

``fault_point(name)`` does nothing unless BOTH hold:

* ``DENSTOCK_DR_FAULT`` names exactly this point, and
* the process runs in development (DEBUG) or test settings.

Production, public-catalog and emergency-local processes never act on the
variable, even when it is set.  The action is chosen by
``DENSTOCK_DR_FAULT_ACTION``:

* ``SIGKILL`` (default) and ``SIGTERM``: the process signals itself, which is a
  real crash: no ``finally`` block, no journal record, no cleanup;
* ``ENOSPC``: raises ``OSError(ENOSPC)`` as a full disk would;
* ``PAUSE``: sleeps ``DENSTOCK_DR_FAULT_PAUSE`` seconds (default 60) so a test
  can observe a held lock or kill the process from outside.
"""

from __future__ import annotations

import errno
import os
import signal
import time

from django.conf import settings

FAULT_ENV = "DENSTOCK_DR_FAULT"
ACTION_ENV = "DENSTOCK_DR_FAULT_ACTION"
PAUSE_ENV = "DENSTOCK_DR_FAULT_PAUSE"


def armed() -> bool:
    mode = str(getattr(settings, "DENSTOCK_MODE", "") or "").strip().lower()
    if mode == "test":
        return True
    return mode == "development" and bool(settings.DEBUG)


def fault_point(name: str) -> None:
    if os.environ.get(FAULT_ENV) != name or not armed():
        return
    action = os.environ.get(ACTION_ENV, "SIGKILL").strip().upper()
    if action == "ENOSPC":
        raise OSError(errno.ENOSPC, "No space left on device (injected)")
    if action == "PAUSE":
        time.sleep(float(os.environ.get(PAUSE_ENV, "60")))
        return
    sig = signal.SIGTERM if action == "SIGTERM" else signal.SIGKILL
    os.kill(os.getpid(), sig)
    time.sleep(30)  # SIGTERM is delivered asynchronously; never run past the point
    raise SystemExit(f"fault {name} did not terminate the process")


def members_with_fault(tar, point: str):
    """Yield tar members, hitting ``point`` after the first one is extracted."""
    for index, member in enumerate(tar):
        if index == 1:
            fault_point(point)
        yield member
