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
    """A minimal, truthful schema.org Product for one part page."""
    facts = card.facts
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": card.display_name,
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


def sitemap_public_ids(number: int) -> list:
    """One bounded sitemap file of part identities, in stable key order."""
    start = (number - 1) * SITEMAP_PAGE_SIZE
    return list(
        public_parts()
        .order_by("pk")
        .values_list("public_id", flat=True)[start : start + SITEMAP_PAGE_SIZE]
    )
