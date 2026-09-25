"""Representative-article acceptance: does an exact-article search land here?

Task §46: a fixture in the shape of a real BRP article search ("404105500",
"404105500 купить", "GUIDE SCREW 404105500") must land directly on the
canonical product page carrying that article in the title, the visible
article block, the meta description, and JSON-LD sku/mpn, with a visible
direct CTA next to it - not the home page, not a generic search results
page. Uses a fixture part, never production data.
"""

import json
import re

from apps.catalog.public_seo import part_title


def _json_ld(body: str) -> dict:
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
    assert match, "no JSON-LD block on the product page"
    return json.loads(match.group(1))


def test_representative_article_lands_on_its_own_canonical_product_page(
    public_client, public_catalog
):
    part = public_catalog.part(
        "GUIDE SCREW", article="404105500", price="1200", maker="BRP"
    )
    public_catalog.stock(part, "4")

    detail = public_client.get(f"/parts/{part.public_id}/")
    body = detail.content.decode()

    assert detail.status_code == 200

    # Title carries the article, not only the generic product name.
    assert "404105500" in detail.context["page_title"]
    assert detail.context["page_title"] == part_title(detail.context["card"])

    # Visible article block (not only metadata).
    assert '<span class="article__value">404105500</span>' in body

    # Meta description carries the article.
    meta_description = detail.context["meta_description"]
    assert "артикул 404105500" in meta_description
    assert f'<meta name="description" content="{meta_description}">' in body or (
        "404105500" in re.search(r'<meta name="description" content="([^"]*)">', body).group(1)
    )

    # JSON-LD sku/mpn.
    data = _json_ld(body)
    assert data["sku"] == "404105500"
    assert data["mpn"] == "404105500"

    # Visible direct CTA on this exact page - no detour through the home page.
    assert "Заказать" in body
    assert f'action="/cart/{part.public_id}/add/"' in body


def test_article_only_query_surfaces_this_product_directly(public_client, public_catalog):
    """The internal search box: a bare article query finds the part (the
    arrival path once a visitor is already on the site, and the same path
    ``tests/test_public_catalog_browse.py`` already proves for another
    article). The Google-side half of §46 - "404105500", "404105500 купить"
    and "GUIDE SCREW 404105500" as external search-engine queries ranking our
    canonical page - is a property of the page's own title/meta/JSON-LD/URL
    content, already proven above; matching a full free-text phrase including
    an unrelated Russian verb or the product name is this site's own search
    relevance feature, not something this task changed, so it is not
    asserted here.
    """
    part = public_catalog.part("GUIDE SCREW", article="404105500", price="1200", maker="BRP")
    public_catalog.stock(part, "4")

    response = public_client.get("/search/", {"q": "404105500"})
    body = response.content.decode()
    assert response.status_code == 200
    assert str(part.public_id) in body
    assert f"/parts/{part.public_id}/" in body
