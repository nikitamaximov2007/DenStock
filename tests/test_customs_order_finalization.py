from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.customs_orders import services
from apps.customs_orders.models import CustomsOrder, CustomsOrderLine
from apps.warehouse.models import ValuationSettings

pytestmark = pytest.mark.django_db


def _row(source_id, *, quantity="1.000", price="10.00", occurred_at=None, analog=False):
    return {
        "source": "sale",
        "source_id": source_id,
        "number": f"ARTICLE-{source_id}",
        "name_ru": "ДЕТАЛЬ",
        "name_en": "PART",
        "manufacturer": "BRP",
        "country": "CANADA",
        "gross_weight_kg": None,
        "net_weight_kg": None,
        "application_area": "СНЕГОХОД",
        "quantity": Decimal(quantity),
        "usd_price": Decimal(price) if price is not None else None,
        "is_analog": analog,
        "provenance": "sales_repairs",
        "occurred_at": occurred_at,
    }


def _finalize(monkeypatch, rows, *, number=125, boundary=2, order_type="original"):
    ValuationSettings.objects.update_or_create(pk=1, defaults={"current_usd_rate": Decimal("100")})
    monkeypatch.setattr(services, "_lock_sources", lambda selected: None)
    monkeypatch.setattr(services, "eligible_customs_sources", lambda *_: rows)
    token = services.selection_payload(rows, order_type=order_type)
    return services.create_customs_order_from_boundary(
        number=number, boundary_source=("sale", boundary), selection_token=token,
        order_type=order_type,
    )


def test_finalization_keeps_the_displayed_prefix_and_snapshot_totals(monkeypatch):
    rows = [_row(1, quantity="1.500", price="10.25"), _row(2, quantity="2", price="4")]

    order = _finalize(monkeypatch, rows)

    assert order.number == 125
    assert order.total_quantity == Decimal("3.500")
    assert order.total_rub == Decimal("2337.50")
    assert list(order.lines.values_list("source", "source_id")) == [("sale", 1), ("sale", 2)]
    assert list(order.lines.values_list("rub_amount", flat=True)) == [
        Decimal("1537.50"), Decimal("800.00")
    ]


def test_stale_source_snapshot_rolls_back_the_whole_order(monkeypatch):
    displayed = [_row(1), _row(2)]
    changed = [_row(1), _row(2, quantity="2")]
    ValuationSettings.objects.update_or_create(pk=1, defaults={"current_usd_rate": Decimal("100")})
    monkeypatch.setattr(services, "_lock_sources", lambda selected: None)
    monkeypatch.setattr(services, "eligible_customs_sources", lambda *_: changed)
    token = services.selection_payload(displayed)

    with pytest.raises(services.CustomsOrderError, match="Состав изменился"):
        services.create_customs_order_from_boundary(
            number=125, boundary_source=("sale", 2), selection_token=token
        )

    assert not CustomsOrder.objects.exists()
    assert not CustomsOrderLine.objects.exists()


def test_later_unseen_source_is_not_silently_added_to_the_order(monkeypatch):
    displayed = [_row(1), _row(2)]
    now = [*displayed, _row(3)]

    order = _finalize(monkeypatch, now, boundary=2)

    assert list(order.lines.values_list("source_id", flat=True)) == [1, 2]


def test_missing_price_fails_closed_without_an_empty_order(monkeypatch):
    rows = [_row(1), _row(2, price=None)]

    with pytest.raises(services.CustomsOrderError, match="Нет оптовой цены"):
        _finalize(monkeypatch, rows)

    assert not CustomsOrder.objects.exists()


def test_finalized_snapshots_cannot_be_edited_or_deleted(monkeypatch):
    order = _finalize(monkeypatch, [_row(1)], boundary=1)
    line = order.lines.get()
    order.total_rub = Decimal("1")
    line.article = "CHANGED"

    with pytest.raises(ValidationError):
        order.save()
    with pytest.raises(ValidationError):
        line.save()
    with pytest.raises(ValidationError):
        order.delete()
    with pytest.raises(ValidationError):
        line.delete()


def test_original_and_analog_series_are_independent(monkeypatch):
    original = _finalize(monkeypatch, [_row(1)], boundary=1, number=125)
    analog = _finalize(
        monkeypatch, [_row(2, analog=True)], boundary=2, number=1, order_type="analog"
    )

    assert original.order_type == CustomsOrder.OrderType.ORIGINAL
    assert analog.order_type == CustomsOrder.OrderType.ANALOG
    assert services.next_order_number(CustomsOrder.OrderType.ORIGINAL) == 126
    assert services.next_order_number(CustomsOrder.OrderType.ANALOG) == 2


def test_same_number_is_allowed_across_types_but_not_within_type():
    CustomsOrder.objects.create(number=1, order_type="original", fx_rate=Decimal("100"))
    CustomsOrder.objects.create(number=1, order_type="analog", fx_rate=Decimal("100"))
    with pytest.raises(IntegrityError):
        CustomsOrder.objects.create(number=1, order_type="original", fx_rate=Decimal("100"))


def test_selected_type_filters_the_canonical_unassigned_queue(monkeypatch):
    rows = [_row(1), _row(2, analog=True), _row(3)]
    monkeypatch.setattr(services, "customs_sources", lambda **_: rows)

    assert [row["source_id"] for row in services.eligible_customs_sources("original")] == [1, 3]
    assert [row["source_id"] for row in services.eligible_customs_sources("analog")] == [2]
