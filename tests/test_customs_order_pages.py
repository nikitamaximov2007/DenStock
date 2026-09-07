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


def test_report_is_an_unassigned_customs_queue(
    client, django_user_model, monkeypatch,
):
    calls = []

    def sources(*, filters, unassigned_only):
        calls.append(unassigned_only)
        assert unassigned_only is True
        return [_source(11)]

    monkeypatch.setattr("apps.actions.views.customs_sources", sources)
    client.force_login(_user(django_user_model))

    response = client.get(reverse("actions_report"))

    html = response.content.decode()
    assert response.status_code == 200
    assert 'class="customs-source--unassigned"' in html
    assert 'customs-source--assigned' not in html
    assert 'name="unassigned"' not in html
    assert html.count("SAME-ARTICLE") == 1
    assert calls == [True]


def test_report_always_requests_the_canonical_unassigned_dataset(
    client, django_user_model, monkeypatch,
):
    calls = []

    def sources(*, filters, unassigned_only):
        calls.append((filters, unassigned_only))
        return [_source(11)]

    monkeypatch.setattr("apps.actions.views.customs_sources", sources)
    client.force_login(_user(django_user_model))

    response = client.get(reverse("actions_report"))

    assert response.status_code == 200
    assert calls[0][1] is True
    assert 'class="customs-source--unassigned"' in response.content.decode()


def test_general_exports_request_only_unassigned_sources(
    client, django_user_model, monkeypatch,
):
    calls = []

    def normal(**kwargs):
        calls.append(("normal", kwargs["unassigned_only"]))
        return []

    def analog(**kwargs):
        calls.append(("analog", kwargs["unassigned_only"]))
        return []

    monkeypatch.setattr("apps.actions.views.historical_customs_rows", normal)
    monkeypatch.setattr("apps.actions.views.historical_analog_customs_rows", analog)
    client.force_login(_user(django_user_model))

    normal_response = client.get(reverse("actions_export"))
    analog_response = client.get(reverse("actions_analog_export"))

    assert normal_response.status_code == 302
    assert analog_response.status_code == 302
    assert calls == [("normal", True), ("analog", True)]


@pytest.mark.parametrize("preset", ("today", "week", "month", "all"))
def test_customs_queue_uses_only_standard_date_presets(
    client, django_user_model, monkeypatch, preset,
):
    calls = []

    def sources(*, filters, unassigned_only):
        calls.append(filters)
        return []

    monkeypatch.setattr("apps.actions.views.customs_sources", sources)
    client.force_login(_user(django_user_model))

    response = client.get(reverse("actions_report"), {"preset": preset})

    html = response.content.decode()
    assert response.status_code == 200
    assert calls
    assert 'name="date_from"' not in html
    assert 'name="date_to"' not in html
    assert f'?preset={preset}' in html


def test_customs_queue_defaults_to_all_time(client, django_user_model, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "apps.actions.views.customs_sources",
        lambda *, filters, unassigned_only: seen.append(filters) or [],
    )
    client.force_login(_user(django_user_model))

    response = client.get(reverse("actions_report"))

    assert response.status_code == 200
    assert seen[0]["date_from"] is None
    assert seen[0]["date_to"] is None


def test_customs_order_quantities_are_integral_and_usd_has_two_decimals(
    client, django_user_model,
):
    order = CustomsOrder.objects.create(
        number=125, order_type="original", fx_rate=Decimal("100"),
        total_quantity=Decimal("2.000"), total_rub=Decimal("3926"),
    )
    CustomsOrderLine.objects.create(
        order=order, source="sale", source_id=42, article="ARTICLE", quantity=Decimal("2.000"),
        wholesale_usd=Decimal("19.6300"), rub_amount=Decimal("3926"),
    )
    client.force_login(_user(django_user_model))

    list_html = client.get(reverse("customs_orders_list")).content.decode()
    detail_html = client.get(reverse("customs_order_detail", args=[order.pk])).content.decode()

    assert "2.000" not in list_html
    assert "2.000" not in detail_html
    assert "19.6300" not in detail_html
    assert "19,63" in detail_html


def test_selection_shows_a_visible_prefix_preview_and_bootstrap_warning(
    client, django_user_model, monkeypatch,
):
    rows = [_source(1), _source(2)]
    monkeypatch.setattr("apps.customs_orders.views.eligible_customs_sources", lambda *_: rows)
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
