#!/usr/bin/env python3
"""Stage 15 HTTP acceptance for a running public catalog host.

Standard library only, so it runs from any workstation against a local
isolated stack, the preview or (after release) production. By default it is
strictly read-only: GET requests only. ``--exercise-cart`` adds one cart
add/remove round trip, which changes nothing but the checker's own cookie,
and ``--probe-post`` also POSTs to the internal paths expecting 404.

``--submit-request`` (with ``--exercise-cart``) instead sends the cart as a
real customer request: it WRITES one request row that operators will see in
their queue. Use it on an isolated stack or the preview; on production only
with the operators' agreement, and cancel the request afterwards.

    python scripts/qualification/public_catalog_acceptance.py \\
        --base-url https://catalog.example --article 420892388 --expect-indexing off

Exit code 0 means every check passed. ``--json`` prints machine-readable
evidence for the release record.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

INTERNAL_PATHS = (
    "/admin/",
    "/login/",
    "/dashboard/",
    "/quick-actions/",
    "/inventory/",
    "/stock/",
    "/sales/",
    "/repairs/",
    "/write-off/",
    "/writeoffs/",
    "/clients/",
    "/customers/",
    "/reports/",
    "/customs/",
    "/directories/",
    "/parts/1/",
    "/parts/public-photos/",
    "/backups/",
    "/api/",
    "/ai-support/",
    "/customer-requests/",
    "/customer-requests/telegram/webhook/",
    "/media/part-types/1/x.jpg",
    "/private_media/x.png",
    "/static/css/app.css",
    "/static/admin/css/base.css",
    "/.env",
    "/.git/config",
)
LEAK_MARKERS = ("Traceback", "django.db", "psycopg", "DenisStock", "SELECT ", "/app/")
PART_LINK = re.compile(r'href="(/parts/[0-9a-f-]{36}/)"')
PHOTO_LINK = re.compile(r'src="(/photos/[0-9a-f-]{36}/(?:card|detail)\.jpg\?v=[0-9a-f]*)"')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Checker:
    def __init__(self, base_url: str, insecure: bool = False):
        self.base = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        context = ssl.create_default_context()
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            NoRedirect(),
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=context),
        )
        self.results: list[dict] = []
        self.timings: list[float] = []

    def fetch(self, path: str, *, method="GET", data=None, headers=None):
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers or {}
        )
        request.add_header("User-Agent", "pro-stor-acceptance/1")
        started = time.monotonic()
        try:
            response = self.opener.open(request, timeout=20)
            status, body, head = response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            status, body, head = error.code, error.read(), error.headers
        self.timings.append((time.monotonic() - started) * 1000)
        return status, head, body

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)


def _text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _post_form(c: Checker, path: str, fields: dict, referer: str):
    payload = urllib.parse.urlencode(fields).encode()
    return c.fetch(
        path,
        method="POST",
        data=payload,
        headers={"Referer": c.base + referer, "Content-Type": "application/x-www-form-urlencoded"},
    )


def _submit_request(c: Checker) -> None:
    """Send the checker's cart as one request (writes one request row)."""
    status, _, body = c.fetch("/request/")
    form = _text(body)
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', form)
    key = re.search(r'name="submission_key" value="([^"]+)"', form)
    if not c.check("request form", status == 200 and bool(csrf and key), str(status)):
        return
    fields = {
        "csrfmiddlewaretoken": csrf.group(1),
        "submission_key": key.group(1),
        "customer_name": "Проверка приёмки PRO-STOR",
        "customer_phone": "+7 900 000-00-00",
        "preferred_messenger": "telegram",
        "comment": "Автоматическая проверка приёмки. Не обрабатывать, отменить.",
        "consent": "1",
        "price": "1",
    }
    status, head, _ = _post_form(c, "/request/submit/", fields, "/request/")
    location = head.get("Location", "")
    c.check(
        "request accepted",
        status == 302 and location.startswith("/request/success/"),
        f"{status} {location}",
    )
    status, retry_head, _ = _post_form(c, "/request/submit/", fields, "/request/")
    c.check("retry returns the same request", retry_head.get("Location") == location)
    status, _, body = c.fetch(location)
    reference = re.search(r"Номер заявки <strong>([0-9A-F]{8})</strong>", _text(body))
    c.check(
        "success page shows the reference",
        status == 200 and bool(reference),
        reference.group(1) if reference else str(status),
    )
    status, _, body = c.fetch("/cart/")
    c.check("cart emptied after the request", "Корзина пуста" in _text(body))


def run(args) -> Checker:
    c = Checker(args.base_url, insecure=args.insecure)

    status, head, body = c.fetch("/")
    html = _text(body)
    c.check("home 200", status == 200, str(status))
    c.check("home search form", 'name="q"' in html and 'role="search"' in html)
    csp = head.get("Content-Security-Policy", "")
    c.check(
        "CSP no scripts, no framing",
        "default-src 'none'" in csp and "frame-ancestors 'none'" in csp,
        csp,
    )
    c.check("X-Frame-Options DENY", head.get("X-Frame-Options") == "DENY")
    c.check("nosniff", head.get("X-Content-Type-Options") == "nosniff")
    c.check("Referrer-Policy", bool(head.get("Referrer-Policy")), head.get("Referrer-Policy", ""))
    cache = head.get("Cache-Control", "")
    c.check("HTML not cacheable", "no-store" in cache and "private" in cache, cache)
    if args.base_url.startswith("https://"):
        c.check("TLS verified", not args.insecure, "certificate verified by the system store")

    robots_status, robots_head, robots_body = c.fetch("/robots.txt")
    robots = _text(robots_body)
    if args.expect_indexing == "off":
        c.check("X-Robots-Tag noindex", "noindex" in head.get("X-Robots-Tag", ""))
        c.check("meta robots noindex", 'content="noindex, nofollow"' in html)
        c.check(
            "robots.txt disallows all", robots_status == 200 and "Disallow: /\n" in robots, robots
        )
    else:
        c.check("no X-Robots-Tag on home", "noindex" not in head.get("X-Robots-Tag", ""))
        c.check("robots.txt allows parts", "Disallow: /search/" in robots and "Sitemap:" in robots)

    css = re.search(r'href="(/static/public_catalog/catalog\.css\?v=[0-9a-f]+)"', html)
    if c.check("public stylesheet linked", bool(css)):
        css_status, css_head, _ = c.fetch(css.group(1))
        c.check(
            "public stylesheet 200", css_status == 200 and "css" in css_head.get("Content-Type", "")
        )

    methods = ("GET", "POST") if args.probe_post else ("GET",)
    for path in INTERNAL_PATHS:
        for method in methods:
            data = b"" if method == "POST" else None
            status, head, body = c.fetch(path, method=method, data=data)
            leaked = [marker for marker in LEAK_MARKERS if marker in _text(body)]
            c.check(
                f"{method} {path} absent",
                status in (404, 405, 403) and "Location" not in head and not leaked,
                f"{status} {leaked}",
            )

    status, head, body = c.fetch("/healthz/")
    c.check(
        "health minimal",
        status == 200 and json.loads(body or b"{}") == {"status": "ok", "db": "ok"},
    )

    # Letters with no common trigram: fuzzy search cannot reach a real name.
    status, _, body = c.fetch("/search/?q=ZXQJWVKQ")
    c.check("no-result search 200", status == 200 and "ничего не нашлось" in _text(body))
    status, _, body = c.fetch("/search/?q=a&page=abc")
    c.check("malformed page is not an error", status == 200)
    status, _, body = c.fetch("/search/?q=" + "x" * 5000)
    c.check("oversized query bounded", status in (200, 414, 400), str(status))
    status, _, body = c.fetch(f"/parts/{uuid.uuid4()}/")
    text = _text(body)
    c.check("unknown part 404 page", status == 404 and "Такой страницы нет" in text)
    c.check("404 leaks nothing", not [m for m in LEAK_MARKERS if m in text])
    status, _, _ = c.fetch(f"/photos/{uuid.uuid4()}/card.jpg")
    c.check("guessed photo 404", status == 404)
    status, _, _ = c.fetch("/photos/..%2F..%2Fetc%2Fpasswd/card.jpg")
    c.check("photo traversal 404", status == 404)

    status, _, body = c.fetch("/sitemap.xml")
    index = _text(body)
    c.check("sitemap index", status == 200 and "<sitemapindex" in index)
    first = re.search(r"<loc>(.*?)</loc>", index)
    if first:
        page_path = urllib.parse.urlparse(first.group(1)).path
        status, _, _ = c.fetch(page_path)
        c.check("first sitemap file", status == 200)

    status, _, _ = c.fetch("/cart/")
    c.check("cart page 200", status == 200)
    status, head, _ = c.fetch("/request/")
    c.check("empty cart has no request form", status == 302 and head.get("Location") == "/cart/")
    status, _, _ = c.fetch(f"/request/success/{uuid.uuid4()}/")
    c.check("foreign request success page 404", status == 404)
    status, _, _ = c.fetch("/request/submit/")
    c.check("request submit is POST only", status == 405, str(status))

    detail_path = None
    if args.article:
        variants = {args.article}
        digits = re.sub(r"[\s\-_./]", "", args.article)
        if digits.isdigit() and len(digits) >= 6:
            variants |= {
                digits,
                f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
                f"{digits[:3]} {digits[3:6]} {digits[6:]}",
            }
        firsts = set()
        for variant in sorted(variants):
            status, _, body = c.fetch("/search/?q=" + urllib.parse.quote(variant))
            links = PART_LINK.findall(_text(body))
            c.check(f"search {variant!r} finds the part", status == 200 and bool(links))
            if links:
                firsts.add(links[0])
        c.check("article variants agree on the first result", len(firsts) == 1, str(firsts))
        detail_path = next(iter(firsts), None)

    if detail_path:
        status, head, body = c.fetch(detail_path)
        text = _text(body)
        c.check("detail 200", status == 200)
        c.check("detail single h1", text.count("<h1") == 1)
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', text)
        c.check("canonical absolute", bool(canonical) and canonical.group(1).startswith("http"))
        ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
        try:
            data = json.loads(ld.group(1)) if ld else None
        except ValueError:
            data = None
        c.check("JSON-LD valid Product", bool(data) and data.get("@type") == "Product")
        if data and "offers" in data:
            c.check(
                "offer has price and availability",
                bool(data["offers"].get("price"))
                and data["offers"].get("availability", "").startswith("https://schema.org/"),
            )
        normalized = re.sub(r"[\s\-_./]", "", args.article).upper()
        c.check("article visible as text", normalized in re.sub(r"[\s\-_./]", "", text).upper())
        for photo in PHOTO_LINK.findall(text)[:2]:
            status, head, _ = c.fetch(photo.replace("&amp;", "&"))
            c.check(
                f"photo {photo[:48]}", status == 200 and head.get("Content-Type") == "image/jpeg"
            )

        if args.exercise_cart:
            token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', text)
            action = re.search(
                r'<form class="offer__form"\s+method="post"\s+action="([^"]+)"', text
            )
            if c.check("cart form present", bool(token and action)):
                payload = urllib.parse.urlencode(
                    {"csrfmiddlewaretoken": token.group(1), "quantity": "1", "price": "1"}
                ).encode()
                status, head, _ = c.fetch(
                    action.group(1),
                    method="POST",
                    data=payload,
                    headers={
                        "Referer": c.base + detail_path,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                c.check("cart add redirects", status == 302, str(status))
                status, _, body = c.fetch("/cart/")
                cart = _text(body)
                c.check("cart shows the line", "cart-line" in cart)
                if args.submit_request:
                    _submit_request(c)
                    return c
                remove = re.search(r'action="(/cart/[0-9a-f-]{36}/remove/)"', cart)
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', cart)
                if remove and token:
                    payload = urllib.parse.urlencode(
                        {"csrfmiddlewaretoken": token.group(1)}
                    ).encode()
                    c.fetch(
                        remove.group(1),
                        method="POST",
                        data=payload,
                        headers={
                            "Referer": c.base + "/cart/",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )
                    status, _, body = c.fetch("/cart/")
                    c.check("cart line removed", "Корзина пуста" in _text(body))
    return c


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--article", help="An article that must be found, e.g. 420892388.")
    parser.add_argument("--expect-indexing", choices=("on", "off"), default="off")
    parser.add_argument("--exercise-cart", action="store_true")
    parser.add_argument(
        "--submit-request",
        action="store_true",
        help="With --exercise-cart: send the cart as a real request (writes one row).",
    )
    parser.add_argument(
        "--probe-post", action="store_true", help="Also POST to internal paths (expects 404)."
    )
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verification.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.submit_request and not (args.exercise_cart and args.article):
        parser.error("--submit-request needs --exercise-cart and --article")
    checker = run(args)
    failed = [result for result in checker.results if not result["ok"]]
    timings = sorted(checker.timings)
    summary = {
        "base_url": args.base_url,
        "checks": len(checker.results),
        "failed": len(failed),
        "requests": len(timings),
        "median_ms": round(timings[len(timings) // 2], 1) if timings else None,
        "p95_ms": round(timings[int(len(timings) * 0.95) - 1], 1) if timings else None,
    }
    if args.json:
        print(
            json.dumps(
                {"summary": summary, "results": checker.results}, ensure_ascii=False, indent=2
            )
        )
    else:
        for result in checker.results:
            mark = "PASS" if result["ok"] else "FAIL"
            print(
                f"{mark}  {result['check']}"
                + (f"  [{result['detail']}]" if not result["ok"] else "")
            )
        print(json.dumps(summary, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
