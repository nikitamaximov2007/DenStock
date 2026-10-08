#!/usr/bin/env python3
"""Freshness monitor: exit 2 and print reasons if any destination is stale/failed."""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.operations import dr_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--label", required=True, action="append")
    parser.add_argument("--max-age-hours", type=float, default=36)
    args = parser.parse_args()
    found = dr_status.problems(
        args.status_file, args.label, timedelta(hours=args.max_age_hours),
    )
    for line in found:
        print(f"DenisStock DR ALERT: {line}", file=sys.stderr)
    return 2 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
