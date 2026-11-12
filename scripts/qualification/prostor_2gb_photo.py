"""Synthetic, bounded photo pipeline load inside a Docker memory cgroup.

Run with a generated PNG/JPEG/WEBP file and --memory=2g --memory-swap=2560m.
No provider account, database row or production data is used.
"""

from __future__ import annotations

import argparse
import gc
import json
import multiprocessing as mp
import os
import time
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from queue import Empty

import django

from apps.customer_requests.attachments import (
    spool_attachment,
    validate_attachment_bytes,
    validate_photo_attachment,
)
from apps.customer_requests.multipart import MultipartBody

CGROUP = Path("/sys/fs/cgroup")


def _number(name: str) -> int | None:
    try:
        value = (CGROUP / name).read_text().strip()
    except OSError:
        return None
    return None if value == "max" else int(value)


def _pss_kib(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def _rss_kib(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def _snapshot(pids: list[int]) -> dict:
    pss = [_pss_kib(pid) for pid in pids]
    rss = [_rss_kib(pid) for pid in pids]
    return {
        "cgroup_mib": round((_number("memory.current") or 0) / 1024**2, 1),
        "swap_mib": round((_number("memory.swap.current") or 0) / 1024**2, 1),
        "pss_mib": round(sum(value or 0 for value in pss) / 1024, 1),
        "rss_mib": round(sum(value or 0 for value in rss) / 1024, 1),
    }


def _worker(image: str, count: int, album_size: int, pipeline: str, ready, start, done,
            release):
    if pipeline == "catalog":
        from apps.catalog.public_photos import build_renditions

    ready.put(True)
    start.wait()
    try:
        processed = 0
        while processed < count:
            with ExitStack() as stack:
                album = []
                for _ in range(min(album_size, count - processed)):
                    data = Path(image).read_bytes()
                    if pipeline == "catalog":
                        assert len(build_renditions(BytesIO(data))) == 2
                    else:
                        validated = validate_photo_attachment(
                            validate_attachment_bytes(data, Path(image).name)
                        )
                        album.append(spool_attachment(validated, stack))
                        body = MultipartBody(b"header", validated.content, b"footer")
                        assert sum(len(chunk) for chunk in body) == len(body)
                        del body, validated
                    del data
                    processed += 1
                for photo in album:
                    assert sum(len(chunk) for chunk in iter(
                        lambda source=photo.file: source.read(64 * 1024), b""
                    )) == photo.size
        gc.collect()
        done.put({"ok": True, "count": processed})
    except Exception as exc:  # report the synthetic failure to the parent
        done.put({"ok": False, "error": type(exc).__name__})
    release.wait()


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    django.setup()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--album-size", type=int, default=1)
    parser.add_argument("--pipeline", choices=("customer", "catalog"), default="customer")
    parser.add_argument("--stabilize-seconds", type=float, default=5)
    args = parser.parse_args()
    if args.count < 1 or args.parallel < 1 or not 1 <= args.album_size <= 12:
        parser.error("count and parallel must be positive; album-size must be 1..12")
    if not Path(args.image).is_file():
        parser.error("synthetic image does not exist")
    ctx = mp.get_context("fork")
    ready, done = ctx.Queue(), ctx.Queue()
    start, release = ctx.Event(), ctx.Event()
    counts = [args.count // args.parallel + (i < args.count % args.parallel)
              for i in range(args.parallel)]
    workers = [ctx.Process(target=_worker, args=(args.image, count, args.album_size,
                                                args.pipeline, ready, start, done, release))
               for count in counts if count]
    for worker in workers:
        worker.start()
    for _ in workers:
        ready.get(timeout=30)
    pids = [worker.pid for worker in workers] + [mp.current_process().pid]
    before = _snapshot(pids)
    peak = before
    begun = time.monotonic()
    start.set()
    results = []
    while len(results) < len(workers):
        current = _snapshot(pids)
        if current["cgroup_mib"] > peak["cgroup_mib"]:
            peak = current
        try:
            results.append(done.get(timeout=0.05))
        except Empty:
            pass
    elapsed = round(time.monotonic() - begun, 2)
    time.sleep(args.stabilize_seconds)
    after = _snapshot(pids)
    release.set()
    for worker in workers:
        worker.join(timeout=10)
    final = _snapshot([mp.current_process().pid])
    events = {}
    for line in (CGROUP / "memory.events").read_text().splitlines():
        key, value = line.split()
        events[key] = int(value)
    report = {
        "count": args.count, "parallel": args.parallel, "album_size": args.album_size,
        "pipeline": args.pipeline,
        "elapsed_seconds": elapsed, "memory_limit_mib": (_number("memory.max") or 0) // 1024**2,
        "swap_limit_mib": (_number("memory.swap.max") or 0) // 1024**2,
        "before": before, "peak": peak, "after_stabilize": after, "after_exit": final,
        "oom": events.get("oom", 0), "oom_kill": events.get("oom_kill", 0),
        "worker_results": results, "orphans": sum(worker.is_alive() for worker in workers),
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if all(result.get("ok") for result in results) and not report["orphans"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
