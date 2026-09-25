"""SEO gaps closed on top of the pre-existing public catalog SEO system.

The bulk of SEO behavior (unique title/description carrying the article,
absolute canonical, JSON-LD offers/brand, global noindex-by-default, the
sitemap index/shard architecture, oil liters-not-pieces on the HTML page) is
already covered by tests/test_public_catalog_pages.py. This file covers only
what this task added: JSON-LD description, oil safety in structured data
specifically, sitemap <lastmod>, GSC/Yandex verification meta, noindex on the
request flow, and a query-count-bounded proof that sitemap generation does
not scale with catalog size.
"""

import re
from decimal import Decimal

import pytest
from django.test import override_settings

from apps.catalog.models import Category, PartType, Unit
from tests.public_catalog_support import assert_no_writes, capture


def _json_ld(body: str) -> dict:
    import json

    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
    assert match, "no JSON-LD block"
    return json.loads(match.group(1))


def _detail(client, part):
    return client.get(f"/parts/{part.public_id}/")


# --- JSON-LD description / oil safety -------------------------------------------------


def test_json_ld_carries_a_description(public_client, public_catalog):
    part = public_catalog.part("BEARING", article="420892388", maker="BRP")
    body = _detail(public_client, part).content.decode()

    data = _json_ld(body)

    assert "артикул 420892388" in data["description"]
    assert "BRP" in data["description"]


def test_json_ld_never_claims_a_piece_quantity_for_oil(public_client, public_catalog):
    part = public_catalog.part("OIL PART", article="OIL-9", price="1000")
    part.is_oil = True
    part.oil_package_volume_l = Decimal("4")
    part.save(update_fields=["is_oil", "oil_package_volume_l"])
    public_catalog.stock(part, "10")

    data = _json_ld(_detail(public_client, part).content.decode())

    # No quantity/unit field anywhere in the schema that could misrepresent
    # litres as a piece count (see §26 - package price stays the authority).
    assert "quantity" not in str(data).lower()
    assert data["offers"]["availability"] == "https://schema.org/InStock"


# --- Sitemap lastmod --------------------------------------------------------------------


def test_sitemap_entries_carry_a_real_lastmod(public_client, public_catalog):
    part = public_catalog.part("Mapped", article="LM-1")
    with override_settings(PUBLIC_CATALOG_BASE_URL="https://pro-brp.ru"):
        page = public_client.get("/sitemaps/parts-1.xml").content.decode()

    expected_date = part.updated_at.date().isoformat()
    assert f"<lastmod>{expected_date}</lastmod>" in page
    # The catalog root has no single trustworthy "changed" timestamp - it
    # must not carry a fabricated one.
    root_line = next(line for line in page.splitlines() if "pro-brp.ru/</loc>" in line)
    assert "<lastmod>" not in root_line


def test_sitemap_lastmod_reflects_a_real_update(public_client, public_catalog):
    part = public_catalog.part("Mapped", article="LM-2")
    part.name = "Mapped renamed"
    part.save(update_fields=["name", "updated_at"])

    with override_settings(PUBLIC_CATALOG_BASE_URL="https://pro-brp.ru"):
        page = public_client.get("/sitemaps/parts-1.xml").content.decode()

    part.refresh_from_db()
    assert f"<lastmod>{part.updated_at.date().isoformat()}</lastmod>" in page


# --- Search engine verification ----------------------------------------------------------


def test_verification_meta_tags_are_absent_by_default(public_client, public_catalog):
    body = public_client.get("/").content.decode()
    assert "google-site-verification" not in body
    assert "yandex-verification" not in body


def test_verification_meta_tags_render_only_when_configured(public_client, public_catalog):
    with override_settings(
        PUBLIC_CATALOG_GOOGLE_SITE_VERIFICATION="google-token-abc",
        PUBLIC_CATALOG_YANDEX_VERIFICATION="yandex-token-xyz",
    ):
        body = public_client.get("/").content.decode()
    assert re.search(r'<meta name="google-site-verification"\s+content="google-token-abc">', body)
    assert re.search(r'<meta name="yandex-verification"\s+content="yandex-token-xyz">', body)


# --- Request flow noindex -----------------------------------------------------------------


TOKEN_RE = re.compile(r'name="submission_key" value="([^"]+)"')


def test_request_form_and_success_pages_are_always_noindex(public_client, public_catalog):
    part = public_catalog.part("BELT", article="RF-1")
    public_catalog.stock(part, "1")

    with override_settings(PUBLIC_CATALOG_INDEXING=True):
        public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
        form = public_client.get("/request/")
        token = TOKEN_RE.search(form.content.decode()).group(1)
        submit = public_client.post(
            "/request/submit/",
            {
                "submission_key": token,
                "customer_name": "Иван Петров",
                "customer_phone": "+7 (912) 123-45-67",
                "preferred_messenger": "telegram",
                "comment": "",
                "consent": "1",
            },
            follow=True,
        )

    assert '<meta name="robots" content="noindex, nofollow">' in form.content.decode()
    assert submit.status_code == 200
    assert '<meta name="robots" content="noindex, nofollow">' in submit.content.decode()


# --- Performance: sitemap generation does not scale with catalog size ---------------------


def _bulk_public_parts(n, *, category, unit):
    PartType.objects.bulk_create(
        [
            PartType(
                name=f"Bulk part {index}",
                category=category,
                unit=unit,
                tracking_mode=PartType.TrackingMode.BULK,
                is_active=True,
                is_public=True,
            )
            for index in range(n)
        ]
    )


def test_sitemap_shard_query_count_does_not_grow_with_catalog_size(db):
    from django.test import Client

    category = Category.objects.create(name="Bulk sitemap category")
    unit = Unit.objects.get(name="Штука")

    _bulk_public_parts(50, category=category, unit=unit)
    with override_settings(
        ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"]
    ):
        client = Client(HTTP_HOST="catalog.example")
        with capture() as small:
            response = client.get("/sitemaps/parts-1.xml")
        assert response.status_code == 200
        assert_no_writes(small)
        small_count = len(small.captured_queries)

        _bulk_public_parts(2000, category=category, unit=unit)
        with capture() as large:
            response = client.get("/sitemaps/parts-1.xml")
        assert response.status_code == 200
        assert_no_writes(large)
        large_count = len(large.captured_queries)

    assert large_count == small_count
    assert large_count <= 3


@pytest.mark.slow
def test_sitemap_generation_is_bounded_at_large_scale(db):
    """A much larger catalog than any single shard needs, proving the shard
    boundary (not catalog size) drives both query count and page count."""
    from django.test import Client

    from apps.catalog import public_seo

    category = Category.objects.create(name="Slow sitemap category")
    unit = Unit.objects.get(name="Штука")
    _bulk_public_parts(12_000, category=category, unit=unit)

    with override_settings(
        ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"]
    ):
        client = Client(HTTP_HOST="catalog.example")
        with capture() as queries:
            index = client.get("/sitemap.xml")
            first_shard = client.get("/sitemaps/parts-1.xml")

    assert index.status_code == 200
    assert first_shard.status_code == 200
    assert index.content.decode().count("<sitemap>") == public_seo.sitemap_page_count()
    # +2: the catalog root and the about page are inserted at the front of
    # the first shard only.
    assert first_shard.content.decode().count("<url>") == (
        min(12_000, public_seo.SITEMAP_PAGE_SIZE) + 2
    )
    assert_no_writes(queries)
    assert len(queries.captured_queries) <= 6
