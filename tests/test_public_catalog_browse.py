from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit


@pytest.fixture
def part(db):
    category = Category.objects.create(name="Public browse category")
    unit = Unit.objects.create(name="Штука для публичного поиска", short_name="шт")
    part = PartType.objects.create(
        name="Public bearing", category=category, unit=unit, recommended_price=Decimal("1250")
    )
    PartNumber.objects.create(
        part=part, value="420-892-388", kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    return part


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_public_search_and_permanent_detail_never_use_internal_id(part):
    client = Client()
    response = client.get(
        reverse("public_catalog_search"), {"q": "420892388"}, HTTP_HOST="catalog.example"
    )
    assert response.status_code == 200
    assert str(part.public_id) in response.content.decode()
    assert f"/parts/{part.pk}/" not in response.content.decode()

    detail = client.get(
        reverse("public_catalog_part", args=[part.public_id]), HTTP_HOST="catalog.example"
    )
    assert detail.status_code == 200
    text = detail.content.decode()
    assert "420-892-388" in text
    assert "1250" in text
    assert 'rel="canonical"' in text


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_public_zero_stock_and_unknown_price_remain_visible(part):
    part.recommended_price = None
    part.save()
    response = Client().get(
        reverse("public_catalog_part", args=[part.public_id]), HTTP_HOST="catalog.example"
    )
    assert response.status_code == 200
    assert "Уточнить цену" in response.content.decode()
    assert "Узнать о поставке" in response.content.decode()


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_public_search_is_not_indexable_and_unknown_identity_is_404(part):
    client = Client()
    search = client.get("/search/?q=420", HTTP_HOST="catalog.example")
    # Indexing is off unless the deployment turns it on; with it on, search
    # result pages stay noindex but let crawlers follow to part pages.
    assert '<meta name="robots" content="noindex, nofollow">' in search.content.decode()
    with override_settings(PUBLIC_CATALOG_INDEXING=True):
        search = client.get("/search/?q=420", HTTP_HOST="catalog.example")
    assert '<meta name="robots" content="noindex, follow">' in search.content.decode()
    unknown = client.get(
        "/parts/00000000-0000-0000-0000-000000000000/", HTTP_HOST="catalog.example"
    )
    assert unknown.status_code == 404


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_anonymous_cart_rechecks_public_part_and_never_uses_a_client_price(part):
    client = Client()
    response = client.post(
        reverse("public_catalog_cart_add", args=[part.public_id]),
        {"quantity": "1", "price": "1"},
        HTTP_HOST="catalog.example",
    )
    assert response.status_code == 302
    cart = client.get(reverse("public_catalog_cart"), HTTP_HOST="catalog.example").content.decode()
    # A part without stock becomes a supply inquiry; the posted price is ignored.
    assert "запрос о поставке" in cart
    assert "1\u00a0250\u00a0₽" in cart and "1\u00a0₽" not in cart

    too_many = client.post(
        reverse("public_catalog_cart_add", args=[part.public_id]),
        {"quantity": "1001"},
        HTTP_HOST="catalog.example",
    )
    assert too_many.status_code == 302
