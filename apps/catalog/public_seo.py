"""Search-engine surface of the public catalog: indexing switch, metadata, JSON-LD.

Indexing is a deployment decision, not a code path. ``PUBLIC_CATALOG_INDEXING``
defaults to off, so a preview or any host that forgot the setting stays
``noindex``. The production launch flips one environment variable (and drops
the edge ``X-Robots-Tag`` header); no template or view changes.

Structured data states only what the catalog actually knows. A price appears
only when the canonical price is known, availability is ``InStock`` only when
something is available right now, and there are no ratings, reviews or
invented brand claims.
"""

from __future__ import annotations

import json
import math

from django.conf import settings

from .public_catalog import PartCard, public_parts

SITE_NAME = "PRO-STOR"
SITEMAP_PAGE_SIZE = 10_000
IN_STOCK = "https://schema.org/InStock"
OUT_OF_STOCK = "https://schema.org/OutOfStock"
_JSON_SCRIPT_ESCAPES = {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}


def indexing_enabled() -> bool:
    return bool(getattr(settings, "PUBLIC_CATALOG_INDEXING", False))


# Owner-confirmed real public accounts (all three). Telegram is also the
# channel docs/research/01-denis-public-channels-full-review.md read and
# tools/research/telegram_collector.py reads. Descriptions stay generic
# ("PRO-STOR on <platform>") rather than characterizing channel content -
# no claim here needs separate proof.
DEFAULT_TELEGRAM_URL = "https://t.me/probrp1"
DEFAULT_VK_URL = "https://vk.ru/club226817030"
DEFAULT_YOUTUBE_URL = "https://www.youtube.com/@pro-stor6592"


def social_links() -> list[dict]:
    """Real public accounts only - never the private request-bot deep links.

    The defaults here (not only in settings) are the single source of truth
    for the confirmed accounts, so they apply consistently across every
    settings module, not only the deployed public-catalog one. An env
    override still wins when set, for a future account change without a
    code deploy.
    """
    platforms = (
        ("telegram", "Telegram", "PUBLIC_CATALOG_TELEGRAM_URL", DEFAULT_TELEGRAM_URL,
         "PRO-STOR в Telegram"),
        ("vk", "VK", "PUBLIC_CATALOG_VK_URL", DEFAULT_VK_URL, "PRO-STOR во ВКонтакте"),
        ("youtube", "YouTube", "PUBLIC_CATALOG_YOUTUBE_URL", DEFAULT_YOUTUBE_URL,
         "PRO-STOR на YouTube"),
    )
    links = []
    for key, label, setting_name, default_url, description in platforms:
        url = str(getattr(settings, setting_name, default_url) or default_url).strip()
        if url:
            links.append({"key": key, "label": label, "url": url, "description": description})
    return links


def base_url(request) -> str:
    """The canonical origin: configured for production, else the request's own."""
    configured = str(getattr(settings, "PUBLIC_CATALOG_BASE_URL", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    return f"{request.scheme}://{request.get_host()}"


def absolute_url(request, path: str) -> str:
    return base_url(request) + path


def robots_txt(request) -> str:
    if not indexing_enabled():
        return "User-agent: *\nDisallow: /\n"
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /search/\n"
        "Disallow: /cart/\n"
        "Disallow: /request/\n"
        f"Sitemap: {absolute_url(request, '/sitemap.xml')}\n"
    )


def part_title(card: PartCard) -> str:
    facts = card.facts
    head = " ".join(value for value in (facts.article, card.display_name) if value)
    if facts.manufacturer:
        head = f"{head}, {facts.manufacturer}"
    return f"{head} · купить в {SITE_NAME}"


def part_description(card: PartCard) -> str:
    facts = card.facts
    parts = [card.display_name]
    if facts.manufacturer:
        parts.append(facts.manufacturer)
    if facts.article:
        parts.append(f"артикул {facts.article}")
    return (
        ", ".join(parts)
        + f". Цена, наличие на складе и подтверждённые аналоги в каталоге {SITE_NAME}."
    )


def product_json_ld(card: PartCard, *, url: str, images: list[str]) -> dict:
    """A minimal, truthful schema.org Product for one part page.

    ``description`` reuses ``part_description`` - one wording, not a second
    text generator. Oil stays safe by omission: there is no ``quantity`` or
    unit field anywhere here, so nothing ever claims a piece count for a
    part sold by the package (see ``card.in_stock``, already litre-aware).
    """
    facts = card.facts
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": card.display_name,
        "description": part_description(card),
        "url": url,
    }
    if facts.russian_name and facts.english_name != facts.russian_name:
        data["alternateName"] = facts.english_name
    if facts.article:
        data["sku"] = facts.article
        if facts.manufacturer:
            # A part number is a manufacturer part number only next to the
            # manufacturer it belongs to.
            data["mpn"] = facts.article
    if facts.manufacturer:
        data["brand"] = {"@type": "Brand", "name": facts.manufacturer}
    if images:
        data["image"] = images
    if facts.price.status == "known" and facts.price.price_rub is not None:
        data["offers"] = {
            "@type": "Offer",
            "url": url,
            "priceCurrency": "RUB",
            "price": f"{facts.price.price_rub:.2f}",
            "availability": IN_STOCK if card.in_stock else OUT_OF_STOCK,
        }
    return data


def json_for_script(data) -> str:
    """JSON that cannot close its <script> element or open another tag."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).translate(
        _JSON_SCRIPT_ESCAPES
    )


def sitemap_page_count() -> int:
    return max(1, math.ceil(public_parts().count() / SITEMAP_PAGE_SIZE))


def sitemap_entries(number: int) -> list[tuple]:
    """One bounded sitemap file: (public_id, lastmod) pairs, stable key order.

    ``updated_at`` (``auto_now``, already selected - no extra query or join)
    is a real signal, not a fabricated one: it only moves when the row
    actually changes. It is not scoped to public-visible fields alone, so it
    can be a little conservative (bumps on an internal-only edit too), but
    that is the honest trade the task allows - "omitting lastmod is better
    than fabricating it", and this is not fabricated.
    """
    start = (number - 1) * SITEMAP_PAGE_SIZE
    return list(
        public_parts()
        .order_by("pk")
        .values_list("public_id", "updated_at")[start : start + SITEMAP_PAGE_SIZE]
    )
