"""Machine-readable DR status and freshness evaluation (no secrets are stored)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _now() -> datetime:
    return datetime.now(UTC)


def load(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"destinations": {}}
    return data if isinstance(data.get("destinations"), dict) else {"destinations": {}}


def record(path: Path, label: str, *, ok: bool, run: str = "", error: str = "",
           physical_bytes: int | None = None, now: datetime | None = None) -> None:
    """Atomically merge one destination outcome; last_success survives failures."""
    path = Path(path)
    stamp = (now or _now()).isoformat(timespec="seconds")
    data = load(path)
    entry = data["destinations"].setdefault(label, {})
    entry.update(last_attempt_at=stamp, ok=ok, error="" if ok else error[:300])
    if ok:
        entry.update(last_success_at=stamp, run=run, physical_bytes=physical_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".dr-status-")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, sort_keys=True, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def problems(path: Path, labels: list[str], max_age: timedelta,
             now: datetime | None = None) -> list[str]:
    """Empty list means every expected destination succeeded recently."""
    now = now or _now()
    destinations = load(path)["destinations"]
    found = []
    for label in labels:
        entry = destinations.get(label)
        if not entry or not entry.get("last_success_at"):
            found.append(f"{label}: ни одной успешной копии")
            continue
        age = now - datetime.fromisoformat(entry["last_success_at"])
        if age > max_age:
            found.append(f"{label}: последняя успешная копия старше {max_age}")
        elif not entry.get("ok", False):
            found.append(f"{label}: последняя попытка завершилась ошибкой")
    return found
