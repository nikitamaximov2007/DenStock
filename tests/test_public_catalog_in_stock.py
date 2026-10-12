"""The crawlable in-stock listing and its sitemap segment.

Before this listing, no public HTML page linked to any product page: the home
page had only a search form, product pages linked only to confirmed analogs,
and the breadcrumb went to /search/, which robots.txt disallows. Product pages
were reachable by a crawler through the sitemap alone. These tests pin the
fix: every in-stock public part has a plain HTML link from a page that the
home page and every page's footer link to, with exactly one URL per page.
"""

import re

import pytest
from django.test import override_settings

from apps.catalog import public_catalog as public_catalog_service
from apps.catalog import public_seo
from tests.public_catalog_support import (  # noqa: F401 - fixtures
    assert_no_writes,
    capture,
    public_catalog,
    public_client,
)

pytestmark = pytest.mark.django_db


def _part_links(body: str) -> set[str]:
    return set(re.findall(r'href="/parts/([0-9a-f-]{36})/"', body))


@pytest.fixture
def stocked(public_catalog):  # noqa: F811
    in_stock = public_catalog.part("Stocked filter", article="707800288")
    public_catalog.stock(in_stock, "2")
    zero = public_catalog.part("Zero stock seal", article="420230001")
    hidden = public_catalog.part("Private stocked", article="420230002", public=False)
    public_catalog.stock(hidden, "5")
    retired = public_catalog.part("Retired stocked", article="420230003", active=False)
    public_catalog.stock(retired, "5")
    return {"in_stock": in_stock, "zero": zero, "hidden": hidden, "retired": retired}


def test_home_and_every_page_footer_link_to_the_in_stock_listing(public_client, stocked):  # noqa: F811
    for path in ("/", "/about/", f"/parts/{stocked['zero'].public_id}/"):
        body = public_client.get(path).content.decode()
        assert 'href="/in-stock/"' in body, path


def test_listing_links_only_public_active_parts_with_stock(public_client, stocked):  # noqa: F811
    response = public_client.get("/in-stock/")
    assert response.status_code == 200
    links = _part_links(response.content.decode())
    assert links == {str(stocked["in_stock"].public_id)}


def test_listing_is_indexable_and_self_canonical(public_client, stocked):  # noqa: F811
    with override_settings(
        PUBLIC_CATALOG_INDEXING=True, PUBLIC_CATALOG_BASE_URL="https://pro-brp.ru"
    ):
        response = public_client.get("/in-stock/")
    body = response.content.decode()
    assert "X-Robots-Tag" not in response
    assert 'name="robots"' not in body
    assert '<link rel="canonical" href="https://pro-brp.ru/in-stock/">' in body


def test_listing_stays_noindex_while_indexing_is_off(public_client, stocked):  # noqa: F811
    response = public_client.get("/in-stock/")
    assert response["X-Robots-Tag"] == "noindex, nofollow"


def test_pagination_has_exactly_one_url_per_page(public_client, public_catalog, monkeypatch):  # noqa: F811
    monkeypatch.setattr(public_catalog_service, "IN_STOCK_PAGE_SIZE", 2)
    parts = [public_catalog.part(f"Stocked {n}", article=f"5000{n}") for n in range(5)]
    for part in parts:
        public_catalog.stock(part, "1")

    first = public_client.get("/in-stock/").content.decode()
    second = public_client.get("/in-stock/?page=2")
    third = public_client.get("/in-stock/?page=3").content.decode()
    assert second.status_code == 200
    second = second.content.decode()

    assert 'href="/in-stock/?page=2" rel="next"' in first
    assert 'href="/in-stock/" rel="prev"' in second
    assert 'href="/in-stock/?page=3" rel="next"' in second
    assert 'href="/in-stock/?page=2" rel="prev"' in third
    assert 'rel="next"' not in third
    assert 'href="http://catalog.example/in-stock/?page=2"' in second  # self-canonical
    seen = _part_links(first) | _part_links(second) | _part_links(third)
    assert seen == {str(part.public_id) for part in parts}

    redirect = public_client.get("/in-stock/?page=1")
    assert redirect.status_code == 301
    assert redirect["Location"] == "/in-stock/"
    for bad in ("4", "0", "01", "abc", "-1", "9999999"):
        assert public_client.get(f"/in-stock/?page={bad}").status_code == 404, bad


def test_empty_stock_renders_an_honest_empty_listing(public_client, public_catalog):  # noqa: F811
    public_catalog.part("Zero stock only", article="420230009")
    response = public_client.get("/in-stock/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "Сейчас на складе ничего нет" in body
    assert not _part_links(body)


def test_listing_reads_only_and_query_count_is_flat(public_client, public_catalog):  # noqa: F811
    def measure():
        with capture() as queries:
            assert public_client.get("/in-stock/").status_code == 200
        assert_no_writes(queries)
        return len(queries.captured_queries)

    part = public_catalog.part("Stocked base", article="600001")
    public_catalog.stock(part, "1")
    small = measure()
    for n in range(8):
        extra = public_catalog.part(f"Stocked more {n}", article=f"60010{n}")
        public_catalog.stock(extra, "1")
    assert measure() == small


def test_in_stock_sitemap_segment_lists_only_in_stock_public_parts(public_client, stocked):  # noqa: F811
    response = public_client.get("/sitemaps/in-stock.xml")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/xml"
    body = response.content.decode()
    locs = re.findall(r"<loc>([^<]+)</loc>", body)
    assert locs == [f"http://catalog.example/parts/{stocked['in_stock'].public_id}/"]
    assert "<lastmod>" in body


def test_sitemap_index_lists_the_segment_first_and_every_shard(public_client, stocked):  # noqa: F811
    body = public_client.get("/sitemap.xml").content.decode()
    locs = re.findall(r"<loc>([^<]+)</loc>", body)
    assert locs[0] == "http://catalog.example/sitemaps/in-stock.xml"
    assert locs[1:] == [
        f"http://catalog.example/sitemaps/parts-{n}.xml"
        for n in range(1, public_seo.sitemap_page_count() + 1)
    ]


def test_first_shard_lists_the_listing_page(public_client, stocked):  # noqa: F811
    body = public_client.get("/sitemaps/parts-1.xml").content.decode()
    assert "<loc>http://catalog.example/in-stock/</loc>" in body


def test_cart_add_from_the_listing_returns_to_the_listing(public_client, stocked):  # noqa: F811
    part = stocked["in_stock"]
    response = public_client.post(
        f"/cart/{part.public_id}/add/", {"quantity": "1", "if_absent": "1", "next": "/in-stock/"}
    )
    assert response.status_code == 302
    assert response["Location"] == "/in-stock/"
