#!/usr/bin/env python3
"""Bounded local load test for the public catalog runtime. NOT for production.

Drives a realistic request mix against a running catalog-web on a loopback
address, with a fixed number of concurrent clients for a fixed time, and
reports throughput, latency percentiles per route and every non-2xx/3xx
status. Standard library only.

    python scripts/qualification/public_catalog_load.py --base-url http://127.0.0.1:8767 \\
        --clients 8 --seconds 60 --article 420-892-388 --name "GASKET" --typo bearng

The target must be a loopback address: the script refuses anything else so it
cannot be pointed at the preview or production by mistake.

``--submit-requests N --confirm-writes`` runs a separate, controlled write
test instead: N customer requests, each from a fresh browser session and its
own documentation-range client address (so the per-address limit does not
mask the throughput), and reports how many were accepted.
"""

from __future__ import annotations

import argparse
import http.client
import json
import random
import re
import statistics
import sys
import threading
import time
import urllib.parse
from collections import defaultdict

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
PART_LINK = re.compile(r'href="(/parts/[0-9a-f-]{36}/)"')
PHOTO_LINK = re.compile(r'src="(/photos/[0-9a-f-]{36}/card\.jpg\?v=[0-9a-f]*)"')
TOKEN = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
SUBMISSION = re.compile(r'name="submission_key" value="([^"]+)"')


class Client:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.connection = http.client.HTTPConnection(host, port, timeout=30)
        self.cookies: dict[str, str] = {}

    def request(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        try:
            self.connection.request(method, path, body=body, headers=headers)
            response = self.connection.getresponse()
            data = response.read()
        except (http.client.HTTPException, OSError):
            self.connection.close()
            self.connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
            raise
        for value in response.headers.get_all("Set-Cookie") or []:
            name, _, rest = value.partition("=")
            self.cookies[name.strip()] = rest.split(";", 1)[0]
        return response.status, data


def discover(client, args):
    """Collect real part and photo links from the running site."""
    parts, photos = set(), set()
    for query in (args.article, args.name, args.partial, args.typo):
        _status, body = client.request("GET", "/search/?q=" + urllib.parse.quote(query))
        text = body.decode("utf-8", "replace")
        parts.update(PART_LINK.findall(text))
        photos.update(link.replace("&amp;", "&") for link in PHOTO_LINK.findall(text))
    if not parts:
        raise SystemExit("No part links discovered; check --article/--name.")
    return sorted(parts), sorted(photos)


def build_mix(args, parts, photos):
    q = urllib.parse.quote
    mix = [
        (10, "home", lambda: ("GET", "/")),
        (15, "search_exact", lambda: ("GET", "/search/?q=" + q(args.article))),
        (
            10,
            "search_normalized",
            lambda: ("GET", "/search/?q=" + q(args.article.replace("-", ""))),
        ),
        (10, "search_partial", lambda: ("GET", "/search/?q=" + q(args.partial))),
        (5, "search_typo", lambda: ("GET", "/search/?q=" + q(args.typo))),
        (
            10,
            "search_filtered",
            lambda: ("GET", "/search/?q=" + q(args.name) + "&in_stock=1&relation=analog&page=2"),
        ),
        (25, "detail", lambda: ("GET", random.choice(parts))),
        (5, "cart_read", lambda: ("GET", "/cart/")),
        (5, "cart_update", None),
        (5, "request_form", lambda: ("GET", "/request/")),
    ]
    if photos:
        mix.append((5, "photo", lambda: ("GET", random.choice(photos))))
    return mix


def _add_to_cart(client, args, part):
    status, body = client.request("GET", part)
    token = TOKEN.search(body.decode("utf-8", "replace"))
    if not token:
        raise RuntimeError("no csrf token")
    form = urllib.parse.urlencode(
        {"csrfmiddlewaretoken": token.group(1), "quantity": "1", "if_absent": "1"}
    )
    status, _ = client.request(
        "POST",
        part.replace("/parts/", "/cart/", 1) + "add/",
        body=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"http://{args.host}:{args.port}{part}",
        },
    )
    return status


def worker(args, mix, parts, deadline, stats, errors, lock):
    client = Client(args.host, args.port)
    # A non-empty cart, so the request form renders instead of redirecting.
    _add_to_cart(client, args, random.choice(parts))
    weights = [weight for weight, _name, _factory in mix]
    while time.monotonic() < deadline:
        _weight, name, factory = random.choices(mix, weights=weights)[0]
        started = time.monotonic()
        try:
            if name == "cart_update":
                status = _add_to_cart(client, args, random.choice(parts))
            else:
                method, path = factory()
                status, _ = client.request(method, path)
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            with lock:
                errors[name].append(type(exc).__name__)
            continue
        elapsed = (time.monotonic() - started) * 1000
        with lock:
            stats[name].append(elapsed)
            if status >= 400:
                errors[name].append(str(status))


def submitter(args, parts, counter, results, lock):
    """One fresh session per request, each from its own client address."""
    while True:
        with lock:
            if counter["next"] >= args.submit_requests:
                return
            number = counter["next"]
            counter["next"] += 1
        client = Client(args.host, args.port)
        address = f"198.51.100.{number % 250 + 1}"
        started = time.monotonic()
        try:
            _add_to_cart(client, args, random.choice(parts))
            _status, body = client.request("GET", "/request/")
            text = body.decode("utf-8", "replace")
            form = urllib.parse.urlencode(
                {
                    "csrfmiddlewaretoken": TOKEN.search(text).group(1),
                    "submission_key": SUBMISSION.search(text).group(1),
                    "customer_name": "LOAD TEST",
                    "customer_phone": "+7 000 000-00-00",
                    "preferred_messenger": "telegram",
                    "comment": "Automated load test - safe to delete",
                    "consent": "1",
                }
            )
            status, _ = client.request(
                "POST",
                "/request/submit/",
                body=form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"http://{args.host}:{args.port}/request/",
                    "X-Forwarded-For": address,
                },
            )
            outcome = "accepted" if status == 302 else str(status)
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            outcome = type(exc).__name__
        with lock:
            results.append((outcome, (time.monotonic() - started) * 1000))


def run_submissions(args, parts):
    counter, results, lock = {"next": 0}, [], threading.Lock()
    threads = [
        threading.Thread(target=submitter, args=(args, parts, counter, results, lock))
        for _ in range(args.clients)
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    duration = time.monotonic() - started
    outcomes = defaultdict(int)
    for outcome, _elapsed in results:
        outcomes[outcome] += 1
    timings = [elapsed for _outcome, elapsed in results]
    summary = {
        "mode": "submit_requests",
        "clients": args.clients,
        "seconds": round(duration, 1),
        "attempted": len(results),
        "outcomes": dict(outcomes),
        "flows_per_second": round(len(results) / duration, 1),
        "flow_median_ms": round(statistics.median(timings), 1) if timings else None,
        "flow_p95_ms": round(percentile(timings, 0.95), 1) if timings else None,
    }
    print(json.dumps(summary, indent=2))
    return 0 if outcomes.get("accepted", 0) == len(results) else 1


def percentile(values, share):
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * share) - 1)] if ordered else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--article", default="420-892-388")
    parser.add_argument("--name", default="GASKET")
    parser.add_argument("--partial", default="8923")
    parser.add_argument("--typo", default="bearng")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--submit-requests",
        type=int,
        default=0,
        help="Instead of the read mix, send this many real requests (writes rows).",
    )
    parser.add_argument("--confirm-writes", action="store_true")
    args = parser.parse_args(argv)
    url = urllib.parse.urlparse(args.base_url)
    if url.hostname not in LOOPBACK:
        raise SystemExit("Load tests run only against a loopback address.")
    if not 1 <= args.clients <= 64 or not 1 <= args.seconds <= 600:
        raise SystemExit("Bounded load only: 1-64 clients, 1-600 seconds.")
    args.host, args.port = url.hostname, url.port or 80

    parts, photos = discover(Client(args.host, args.port), args)
    if args.submit_requests:
        if not args.confirm_writes or not 1 <= args.submit_requests <= 5000:
            raise SystemExit("--submit-requests needs --confirm-writes and 1-5000 requests.")
        return run_submissions(args, parts)
    mix = build_mix(args, parts, photos)
    stats, errors, lock = defaultdict(list), defaultdict(list), threading.Lock()
    deadline = time.monotonic() + args.seconds
    threads = [
        threading.Thread(target=worker, args=(args, mix, parts, deadline, stats, errors, lock))
        for _ in range(args.clients)
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    duration = time.monotonic() - started

    routes = {}
    for name, values in sorted(stats.items()):
        routes[name] = {
            "requests": len(values),
            "median_ms": round(statistics.median(values), 1),
            "p95_ms": round(percentile(values, 0.95), 1),
            "p99_ms": round(percentile(values, 0.99), 1),
            "max_ms": round(max(values), 1),
            "errors": len(errors.get(name, [])),
        }
    total = sum(len(values) for values in stats.values())
    summary = {
        "clients": args.clients,
        "seconds": round(duration, 1),
        "requests": total,
        "rps": round(total / duration, 1),
        "errors": sum(len(values) for values in errors.values()),
        "error_kinds": {name: sorted(set(values)) for name, values in errors.items()},
        "routes": routes,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"clients={args.clients} seconds={summary['seconds']} requests={total} "
            f"rps={summary['rps']} errors={summary['errors']}"
        )
        for name, row in routes.items():
            print(
                f"  {name:<18} n={row['requests']:>6} p50={row['median_ms']:>7} "
                f"p95={row['p95_ms']:>7} p99={row['p99_ms']:>7} max={row['max_ms']:>8} "
                f"err={row['errors']}"
            )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
