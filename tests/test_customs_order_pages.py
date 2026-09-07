from decimal import Decimal

import pytest
from django.urls import reverse

from apps.customs_orders.models import CustomsOrder, CustomsOrderLine

pytestmark = pytest.mark.django_db


def _user(django_user_model):
    return django_user_model.objects.create_superuser(username="customs", password="secret")


def _source(source_id, membership=None):
    return {
        "source": "sale",
        "source_id": source_id,
        "number": "SAME-ARTICLE",
        "name_ru": "ДЕТАЛЬ",
        "name_en": "PART",
        "manufacturer": "BRP",
        "country": "CANADA",
        "gross_weight_kg": None,
        "net_weight_kg": None,
        "application_area": "СНЕГОХОД",
        "quantity": Decimal("1.000"),
        "usd_price": Decimal("10.00"),
        "is_analog": False,
        "provenance": "sales_repairs",
        "occurred_at": None,
        "membership": membership,
        "document_number": "SALE-1",
    }


def test_report_keeps_assigned_source_visible_with_its_exact_order_link(
    client, django_user_model, monkeypatch,
):
    order = CustomsOrder.objects.create(number=125, fx_rate=Decimal("100"))
    membership = CustomsOrderLine.objects.create(
        order=order, source="sale", source_id=10, article="SAME-ARTICLE",
        quantity=Decimal("1"), wholesale_usd=Decimal("10"), rub_amount=Decimal("100"),
    )
    calls = []

    def sources(*, filters, unassigned_only):
        calls.append(unassigned_only)
        return [_source(10, membership), _source(11)]

    monkeypatch.setattr("apps.actions.views.customs_sources", sources)
    client.force_login(_user(django_user_model))

    response = client.get(reverse("actions_report"))

    html = response.content.decode()
    assert response.status_code == 200
    assert 'class="customs-source--assigned"' in html
    assert 'class="customs-source--unassigned"' in html
    assert reverse("customs_order_detail", args=[order.pk]) in html
    assert "Заказ №125" in html
    assert html.count("SAME-ARTICLE") == 2
    assert calls == [False]


def test_report_unassigned_filter_requests_the_canonical_unassigned_dataset(
    client, django_user_model, monkeypatch,
):
    calls = []

    def sources(*, filters, unassigned_only):
        calls.append((filters, unassigned_only))
        return [_source(11)]

    monkeypatch.setattr("apps.actions.views.customs_sources", sources)
    client.force_login(_user(django_user_model))

    response = client.get(reverse("actions_report"), {"unassigned": "1"})

    assert response.status_code == 200
    assert calls[0][1] is True
    assert 'class="customs-source--unassigned"' in response.content.decode()


def test_selection_shows_a_visible_prefix_preview_and_bootstrap_warning(
    client, django_user_model, monkeypatch,
):
    rows = [_source(1), _source(2)]
    monkeypatch.setattr("apps.customs_orders.views.eligible_customs_sources", lambda: rows)
    monkeypatch.setattr(
        "apps.customs_orders.views.current_fx_rate", lambda: Decimal("100.0000")
    )
    client.force_login(_user(django_user_model))

    response = client.get(reverse("customs_order_selection"))

    html = response.content.decode()
    assert response.status_code == 200
    assert "Выберите крайнюю деталь в заказе" in html
    assert "Выбрано строк" in html
    assert "Введите номер заказа" in html
    assert "История таможенных заказов ещё не инициализирована" in html
    assert 'value="sale:1"' in html
    assert 'value="sale:2"' in html


def test_customs_pages_require_login(client):
    for url in (
        reverse("customs_orders_list"),
        reverse("customs_order_selection"),
    ):
        response = client.get(url)
        assert response.status_code == 302
