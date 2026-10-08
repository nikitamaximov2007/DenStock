#!/usr/bin/env python3
"""Upload the newest staged DR generation to each cloud destination independently.

Host-level (rclone + cryptography only, no Django).  Each destination is
rotated and verified on its own; one destination's failure or rotation can never
touch another's namespace.  Exit status is non-zero if ANY destination failed.
"""

import argparse
import fcntl
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.operations import dr_status  # noqa: E402
from apps.operations.backup_budget import BudgetError  # noqa: E402
from apps.operations.dr_local import ENCRYPTED_NAME, RECEIPT_NAME, RUN_NAME  # noqa: E402
from apps.operations.dr_remote import (  # noqa: E402
    RcloneDestination,
    assert_isolated,
    publish_generation,
    validate_remote,
)


def newest_staged(staging: Path) -> Path:
    runs = sorted(
        p for p in staging.iterdir()
        if p.is_dir() and RUN_NAME.fullmatch(p.name)
        and (p / RECEIPT_NAME).is_file() and (p / ENCRYPTED_NAME).is_file()
    )
    if not runs:
        raise BudgetError("Нет подготовленного DR-поколения (dr_encrypt).")
    return runs[-1]


def parse_destination(value: str) -> tuple[str, str, str]:
    """``label=kind=remote:path`` e.g. ``yandex=s3=yandex-s3:bucket/dr``."""
    try:
        label, kind, remote = value.split("=", 2)
    except ValueError as exc:
        raise BudgetError("Формат назначения: label=kind=remote:path") from exc
    return label, kind, validate_remote(kind, remote)


def run(staging: Path, destinations: list[str], public_key: Path, status_file: Path) -> int:
    parsed = [parse_destination(item) for item in destinations]
    assert_isolated([remote for _, _, remote in parsed])
    if len({label for label, _, _ in parsed}) != len(parsed):
        raise BudgetError("Метки назначений должны быть уникальны.")
    lock_path = staging / ".upload.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        generation = newest_staged(staging)
        receipt = json.loads((generation / RECEIPT_NAME).read_text())
        failures = 0
        for label, kind, remote in parsed:
            try:
                result = publish_generation(
                    RcloneDestination(kind, remote, public_key),
                    generation.name, generation / ENCRYPTED_NAME, receipt,
                )
                dr_status.record(
                    status_file, label, ok=True, run=generation.name,
                    physical_bytes=result["bytes"],
                )
                print(f"{label}: ok {generation.name} bytes={result['bytes']}")
            except BudgetError as exc:
                failures += 1
                dr_status.record(status_file, label, ok=False, error=str(exc))
                print(f"{label}: FAILED {exc}", file=sys.stderr)
        return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--destination", required=True, action="append")
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--status-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        return run(args.staging, args.destination, args.public_key, args.status_file)
    except (BudgetError, BlockingIOError, OSError, ValueError) as exc:
        print(f"DenisStock DR upload failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
