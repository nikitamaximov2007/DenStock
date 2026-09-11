from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customer_requests.models import CustomerRequest
from apps.inventory.availability import available_totals
from apps.inventory.models import StockBalance, StockMovement
from apps.sales.models import Reservation, Sale


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
    assert "noindex,follow" in search.content.decode()
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
    cart = client.get(reverse("public_catalog_cart"), HTTP_HOST="catalog.example")
    assert "Корзина пуста" in cart.content.decode()

    too_many = client.post(
        reverse("public_catalog_cart_add", args=[part.public_id]),
        {"quantity": "1001"},
        HTTP_HOST="catalog.example",
    )
    assert too_many.status_code == 302


def _make_public(part):
    part.is_public = True
    part.is_active = True
    part.save(update_fields=["is_public", "is_active"])


def _put_in_cart(client, part, quantity=1):
    session = client.session
    session["public_catalog_cart"] = {str(part.public_id): quantity}
    session.save()


def _request_token(client):
    return client.session["public_catalog_request_submission"]["token"]


def _valid_form_data(token, **extra):
    return {
        "submission_key": token,
        "customer_name": "Иван Петров",
        "customer_phone": "+7 (912) 123-45-67",
        "preferred_messenger": "telegram",
        "comment": "Нужна деталь.",
        "consent": "1",
        **extra,
    }


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_cart_request_uses_current_price_and_ignores_tampered_fields(part, monkeypatch):
    _make_public(part)
    client = Client()
    _put_in_cart(client, part)
    form = client.get(reverse("public_catalog_request_form"), HTTP_HOST="catalog.example")
    assert form.status_code == 200
    token = _request_token(client)
    part.recommended_price = Decimal("11000.00")
    part.save(update_fields=["recommended_price"])
    monkeypatch.setattr(
        "apps.customer_requests.services.available_totals", lambda ids: {part.pk: Decimal("2")}
    )

    response = client.post(
        reverse("public_catalog_request_submit"),
        _valid_form_data(
            token,
            price="1",
            price_seen="1",
            part_id="999999",
            status="completed",
            privacy_policy_version="forged",
        ),
        HTTP_HOST="catalog.example",
    )

    assert response.status_code == 302
    request = CustomerRequest.objects.get()
    line = request.lines.get()
    assert request.status == CustomerRequest.Status.NEW
    assert request.privacy_policy_version == "draft-legal-review-1"
    assert line.part_type_id == part.pk
    assert line.price_seen == Decimal("11000.00")


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_cart_request_rechecks_availability_and_requires_consent(part, monkeypatch):
    _make_public(part)
    client = Client()
    _put_in_cart(client, part, quantity=3)
    client.get(reverse("public_catalog_request_form"), HTTP_HOST="catalog.example")
    token = _request_token(client)
    monkeypatch.setattr(
        "apps.customer_requests.services.available_totals", lambda ids: {part.pk: Decimal("2")}
    )
    rejected = client.post(
        reverse("public_catalog_request_submit"),
        _valid_form_data(token),
        HTTP_HOST="catalog.example",
    )
    assert rejected.status_code == 400
    assert "Сейчас доступно" in rejected.content.decode()
    assert CustomerRequest.objects.count() == 0

    no_consent = client.post(
        reverse("public_catalog_request_submit"),
        _valid_form_data(token, consent=""),
        HTTP_HOST="catalog.example",
    )
    assert no_consent.status_code == 400
    assert CustomerRequest.objects.count() == 0


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_zero_stock_supply_request_is_non_reserving_and_idempotent(part):
    _make_public(part)
    client = Client()
    before = {
        "balances": StockBalance.objects.count(),
        "movements": StockMovement.objects.count(),
        "reservations": Reservation.objects.count(),
        "sales": Sale.objects.count(),
        "available": available_totals([part.pk]),
    }
    form = client.get(
        f"{reverse('public_catalog_request_form')}?supply={part.public_id}",
        HTTP_HOST="catalog.example",
    )
    assert form.status_code == 200
    assert "Узнать о поставке" in form.content.decode()
    token = _request_token(client)
    first = client.post(
        reverse("public_catalog_request_submit"),
        _valid_form_data(token, preferred_messenger="max"),
        HTTP_HOST="catalog.example",
    )
    retry = client.post(
        reverse("public_catalog_request_submit"),
        _valid_form_data(token, preferred_messenger="max", comment="подменён"),
        HTTP_HOST="catalog.example",
    )
    assert first.status_code == retry.status_code == 302
    request = CustomerRequest.objects.get()
    assert request.lines.get().is_supply_inquiry is True
    assert CustomerRequest.objects.count() == 1
    assert {
        "balances": StockBalance.objects.count(),
        "movements": StockMovement.objects.count(),
        "reservations": Reservation.objects.count(),
        "sales": Sale.objects.count(),
        "available": available_totals([part.pk]),
    } == before
