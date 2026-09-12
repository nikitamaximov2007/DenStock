"""Public pages: home, part detail, SEO metadata, structured data and errors."""

import json
import logging
import re
from decimal import Decimal
from unittest import mock

import pytest
from django.db import OperationalError
from django.test import override_settings

from apps.catalog.public_photos import publish_photo
from tests.public_catalog_support import (
    PUBLIC_HOST,
    assert_no_writes,
    capture,
)


def _json_ld(body: str) -> dict:
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
    assert match, "no JSON-LD block"
    return json.loads(match.group(1))


def _detail(client, part):
    return client.get(f"/parts/{part.public_id}/")


# --- Home ---------------------------------------------------------------------------


def test_home_is_search_first(public_client):
    response = public_client.get("/")
    body = response.content.decode()
    assert response.status_code == 200
    assert body.count("<h1") == 1
    assert 'name="q"' in body and 'role="search"' in body
    assert '<label class="search__label" for="q">' in body
    assert "—" not in body


# --- Part detail ---------------------------------------------------------------------


def test_detail_shows_every_customer_fact_without_internal_ids(public_client, public_catalog):
    part = public_catalog.part(
        "PISTON ASS'Y WITH RINGS",
        article="420892388",
        price="30047",
        maker="BRP",
        russian="Поршень в сборе с кольцами",
    )
    public_catalog.stock(part, "3")

    response = _detail(public_client, part)
    body = response.content.decode()

    assert response.status_code == 200
    assert body.count("<h1") == 1
    assert '<h1 class="part__title">Поршень в сборе с кольцами</h1>' in body
    assert "PISTON ASS&#x27;Y WITH RINGS" in body
    assert "420892388" in body and "BRP" in body
    assert "30\u00a0047\u00a0₽" in body
    assert "В наличии: 3 шт" in body
    assert f"/parts/{part.pk}/" not in body
    assert "S09-D01-C01" not in body, "warehouse cell leaked"
    assert "Public catalog supplier" not in body, "supplier leaked"
    assert "—" not in body


def test_unconfirmed_russian_name_is_never_shown(public_client, public_catalog):
    part = public_catalog.part(
        "SPARK PLUG", article="SP-1", russian="Свеча выдуманная", russian_confirmed=False
    )
    body = _detail(public_client, part).content.decode()
    assert "Свеча выдуманная" not in body
    assert '<h1 class="part__title">SPARK PLUG</h1>' in body


def test_zero_stock_and_unknown_price_are_useful_not_dead_ends(public_client, public_catalog):
    part = public_catalog.part("DRIVE BELT", article="420931785", price=None)

    body = _detail(public_client, part).content.decode()

    assert "Уточнить цену" in body
    assert "Сейчас нет на складе" in body
    assert "Узнать о поставке" in body
    assert f'action="/cart/{part.public_id}/add/"' in body


def test_detail_shows_confirmed_relations_in_both_directions(public_client, public_catalog):
    original = public_catalog.part("OEM PUMP", article="OEM-1")
    analog = public_catalog.part("AFTERMARKET PUMP", article="AM-1", price="500")
    guessed = public_catalog.part("GUESSED PUMP", article="AM-2")
    public_catalog.stock(analog, "4")
    public_catalog.analog(original, analog)
    public_catalog.analog(original, guessed, confirmed=False)

    original_page = _detail(public_client, original).content.decode()
    analog_page = _detail(public_client, analog).content.decode()

    assert "Подтверждённые аналоги" in original_page
    assert "AFTERMARKET PUMP" in original_page and "В наличии: 4 шт" in original_page
    assert "GUESSED PUMP" not in original_page
    assert "Оригинальная деталь" in analog_page and "OEM PUMP" in analog_page


def test_detail_without_analogs_says_so(public_client, public_catalog):
    part = public_catalog.part("LONELY GASKET", article="LG-1")
    assert (
        "Подтверждённых аналогов для этой детали пока нет"
        in _detail(public_client, part).content.decode()
    )


def test_unknown_and_hidden_parts_are_404_pages(public_client, public_catalog):
    hidden = public_catalog.part("Hidden", article="H-1", public=False)
    retired = public_catalog.part("Retired", article="R-1", active=False)
    for path in (
        "/parts/00000000-0000-0000-0000-000000000000/",
        f"/parts/{hidden.public_id}/",
        f"/parts/{retired.public_id}/",
        f"/parts/{hidden.pk}/",
    ):
        response = public_client.get(path)
        assert response.status_code == 404, path
        body = response.content.decode()
        assert "Такой страницы нет" in body
        assert "Traceback" not in body and "catalog_parttype" not in body


@pytest.mark.parametrize("relations", [1, 20, 50])
def test_detail_query_count_is_bounded_and_read_only(
    public_client, public_catalog, relations, record_property
):
    original = public_catalog.part("Bounded original", article="BO-1")
    for index in range(relations):
        analog = public_catalog.part(f"Bounded analog {index}", article=f"BA-{index}")
        public_catalog.stock(analog, "1")
        public_catalog.analog(original, analog)
        publish_photo(public_catalog.image(analog), source="own", by=public_catalog.user)
    publish_photo(public_catalog.image(original), source="own", by=public_catalog.user)

    with capture() as queries:
        response = _detail(public_client, original)

    assert response.status_code == 200
    assert response.content.decode().count("part-card--compact") == relations
    assert_no_writes(queries)
    record_property(f"public_detail_queries_{relations}", len(queries.captured_queries))
    assert len(queries.captured_queries) <= 24


# --- SEO ------------------------------------------------------------------------------


def test_titles_and_descriptions_are_unique_and_carry_the_article(public_client, public_catalog):
    first = public_catalog.part("BEARING", article="420892388", maker="BRP")
    second = public_catalog.part("BEARING", article="420892389", maker="BRP")

    pages = [_detail(public_client, part).content.decode() for part in (first, second)]
    titles = [re.search(r"<title>\s*(.*?)\s*</title>", page, re.S).group(1) for page in pages]
    descriptions = [
        re.search(r'<meta name="description"\s+content="(.*?)"', page, re.S).group(1)
        for page in pages
    ]

    assert titles[0] != titles[1]
    assert titles[0].startswith("420892388 BEARING, BRP")
    assert "купить" in titles[0]
    assert descriptions[0] != descriptions[1]
    assert "артикул 420892388" in descriptions[0]


def test_canonical_is_absolute_and_uses_the_configured_host(public_client, public_catalog):
    part = public_catalog.part("BELT", article="B-1")
    body = _detail(public_client, part).content.decode()
    assert f'<link rel="canonical" href="http://{PUBLIC_HOST}/parts/{part.public_id}/">' in body

    with override_settings(PUBLIC_CATALOG_BASE_URL="https://pro-stor.ru"):
        body = _detail(public_client, part).content.decode()
    assert f'<link rel="canonical" href="https://pro-stor.ru/parts/{part.public_id}/">' in body


def test_structured_data_is_valid_and_truthful(public_client, public_catalog):
    stocked = public_catalog.part("OIL FILTER", article="OF-9", price="1234.50", maker="BRP")
    public_catalog.stock(stocked, "2")
    empty = public_catalog.part("AIR FILTER", article="AF-9", price="990", maker="BRP")
    unknown = public_catalog.part("FUEL FILTER", article="FF-9", price=None, maker=None)

    stocked_ld = _json_ld(_detail(public_client, stocked).content.decode())
    empty_ld = _json_ld(_detail(public_client, empty).content.decode())
    unknown_ld = _json_ld(_detail(public_client, unknown).content.decode())

    assert stocked_ld["@type"] == "Product"
    assert stocked_ld["offers"] == {
        "@type": "Offer",
        "url": stocked_ld["url"],
        "priceCurrency": "RUB",
        "price": "1234.50",
        "availability": "https://schema.org/InStock",
    }
    assert stocked_ld["brand"] == {"@type": "Brand", "name": "BRP"}
    assert stocked_ld["mpn"] == stocked_ld["sku"] == "OF-9"
    assert empty_ld["offers"]["availability"] == "https://schema.org/OutOfStock"
    # Unknown price: no offer at all. Unknown manufacturer: no brand or MPN.
    assert "offers" not in unknown_ld
    assert "brand" not in unknown_ld and "mpn" not in unknown_ld
    for data in (stocked_ld, empty_ld, unknown_ld):
        assert not {"aggregateRating", "review"} & set(data)


def test_structured_data_cannot_break_out_of_its_script(public_client, public_catalog):
    part = public_catalog.part("EVIL </script><script>alert(1)</script> & CO", article="X-1")
    body = _detail(public_client, part).content.decode()
    assert "<script>alert(1)" not in body
    assert _json_ld(body)["name"].startswith("EVIL </script>")


def test_noindex_is_the_default_everywhere(public_client, public_catalog):
    part = public_catalog.part("BELT", article="B-2")
    for path in ("/", f"/parts/{part.public_id}/", "/search/?q=belt", "/cart/"):
        response = public_client.get(path)
        assert response["X-Robots-Tag"] == "noindex, nofollow", path
        assert '<meta name="robots" content="noindex, nofollow">' in response.content.decode()
    robots = public_client.get("/robots.txt").content.decode()
    assert robots == "User-agent: *\nDisallow: /\n"


def test_indexing_switch_opens_part_pages_only(public_client, public_catalog):
    part = public_catalog.part("BELT", article="B-3")
    with override_settings(
        PUBLIC_CATALOG_INDEXING=True, PUBLIC_CATALOG_BASE_URL="https://pro-stor.ru"
    ):
        detail = _detail(public_client, part)
        search = public_client.get("/search/", {"q": "belt"})
        cart = public_client.get("/cart/")
        robots = public_client.get("/robots.txt").content.decode()

    assert "X-Robots-Tag" not in detail
    assert 'name="robots"' not in detail.content.decode()
    assert '<meta name="robots" content="noindex, follow">' in search.content.decode()
    assert '<meta name="robots" content="noindex, nofollow">' in cart.content.decode()
    assert "Disallow: /search/" in robots and "Disallow: /cart/" in robots
    assert "Disallow: /request/" in robots
    assert "Sitemap: https://pro-stor.ru/sitemap.xml" in robots


def test_sitemap_is_an_index_of_bounded_files(public_client, public_catalog):
    visible = public_catalog.part("Mapped", article="M-1")
    hidden = public_catalog.part("Unmapped", article="M-2", public=False)
    with override_settings(PUBLIC_CATALOG_BASE_URL="https://pro-stor.ru"):
        index = public_client.get("/sitemap.xml")
        page = public_client.get("/sitemaps/parts-1.xml")
        missing = public_client.get("/sitemaps/parts-2.xml")

    assert index.status_code == 200 and index["Content-Type"] == "application/xml"
    assert "<loc>https://pro-stor.ru/sitemaps/parts-1.xml</loc>" in index.content.decode()
    body = page.content.decode()
    assert "<loc>https://pro-stor.ru/</loc>" in body
    assert f"<loc>https://pro-stor.ru/parts/{visible.public_id}/</loc>" in body
    assert str(hidden.public_id) not in body
    assert missing.status_code == 404
    assert "max-age=3600" in page["Cache-Control"]


def test_sitemap_splits_at_the_page_size(public_client, public_catalog, monkeypatch):
    from apps.catalog import public_seo

    monkeypatch.setattr(public_seo, "SITEMAP_PAGE_SIZE", 2)
    parts = [public_catalog.part(f"Split {index}", article=f"SP-{index}") for index in range(5)]

    index = public_client.get("/sitemap.xml").content.decode()
    last = public_client.get("/sitemaps/parts-3.xml").content.decode()

    assert index.count("<sitemap>") == 3
    assert last.count("<url>") == 1 and str(parts[-1].public_id) in last


# --- Response policy -----------------------------------------------------------------


def test_html_is_never_cached_and_carries_the_security_headers(public_client, public_catalog):
    part = public_catalog.part("BELT", article="B-4")
    for path in ("/", "/search/?q=belt", f"/parts/{part.public_id}/", "/cart/"):
        response = public_client.get(path)
        cache = response["Cache-Control"]
        assert "no-store" in cache and "private" in cache, path
        policy = response["Content-Security-Policy"]
        # Скрипт на публичных страницах ровно один - маска телефона в форме
        # заявки, и только свой: ни встроенного кода, ни внешних источников.
        assert "script-src 'self'" in policy
        assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
        assert "default-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy
        assert response["X-Frame-Options"] == "DENY"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Referrer-Policy"] == "same-origin"
        assert response["X-Request-ID"]


def test_cart_cookie_is_http_only_and_same_site(public_client, public_catalog):
    part = public_catalog.part("BELT", article="B-5")
    public_catalog.stock(part, "1")
    response = public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
    cookie = response.cookies["prostor_cart"]
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    assert "sessionid" not in response.cookies


def test_database_failure_is_a_calm_503_without_details(public_client, public_catalog):
    with mock.patch(
        "apps.catalog.public_views.search_catalog",
        side_effect=OperationalError('connection to server at "db" failed: password=secret'),
    ):
        response = public_client.get("/search/", {"q": "belt"})
    body = response.content.decode()
    assert response.status_code == 503
    assert response["Retry-After"] == "30"
    assert "Каталог временно недоступен" in body
    for leak in ("password", "secret", "OperationalError", "Traceback", "db"):
        assert leak not in body.split("<main", 1)[1], leak


def test_unexpected_error_page_is_generic(public_catalog):
    from django.test import Client

    from tests.public_catalog_support import public_runtime_settings

    with (
        public_runtime_settings(),
        mock.patch(
            "apps.catalog.public_views.search_catalog", side_effect=RuntimeError("secret detail")
        ),
    ):
        client = Client(HTTP_HOST=PUBLIC_HOST, raise_request_exception=False)
        response = client.get("/search/", {"q": "belt"})
    assert response.status_code == 500
    body = response.content.decode()
    assert "Что-то пошло не так" in body
    assert "secret detail" not in body and "RuntimeError" not in body


def test_access_log_has_route_status_latency_and_no_query(public_client, public_catalog, caplog):
    public_catalog.part("BELT", article="B-6")
    with caplog.at_level(logging.INFO, logger="apps.catalog.public.access"):
        public_client.get("/search/", {"q": "секретный запрос +79990001122"})
    lines = [record.getMessage() for record in caplog.records]
    assert any(
        re.fullmatch(r"route=public_catalog_search status=200 ms=\d+\.\d", line) for line in lines
    )
    joined = " ".join(lines)
    assert "секретный" not in joined and "79990001122" not in joined


def test_health_is_minimal(public_client):
    response = public_client.get("/healthz/")
    assert response.json() == {"status": "ok", "db": "ok"}
    assert "no-store" in response["Cache-Control"]


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_read_pages_reject_writes(public_client, method):
    assert getattr(public_client, method)("/search/").status_code == 405


def test_price_is_shown_as_canonical_decimal_without_rounding(public_client, public_catalog):
    part = public_catalog.part("ODD PRICE", article="OP-1", price="1234.50")
    assert "1\u00a0234,50\u00a0₽" in _detail(public_client, part).content.decode()
    part.recommended_price = Decimal("1235")
    part.save(update_fields=["recommended_price"])
    assert "1\u00a0235\u00a0₽" in _detail(public_client, part).content.decode()
