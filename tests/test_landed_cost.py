"""Слой 7 — landed cost и фиксация себестоимости.

Покрывает обязательные проверки плана 07-layer-7-landed-cost.md §12.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import (
    LandedCostError,
    compute_landed_cost,
    finalize_cost,
)
from apps.suppliers.models import Supplier

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def refs(db):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    part = PartType.objects.create(
        name="Насос", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    return {"sup": sup, "part": part}


_MONEY_KW = {
    "goods_total", "shipping_cost", "customs_cost", "commission_cost",
    "other_cost", "exchange_rate",
}


def _accepted_batch(refs, lines, *, status=Batch.Status.ACCEPTED, **over):
    """Партия с историческими строками. lines = [(quantity, unit_cost_currency), ...]."""
    over = {k: (Decimal(v) if k in _MONEY_KW else v) for k, v in over.items()}
    batch = Batch.objects.create(supplier=refs["sup"], **over)
    for qty, unit_cost in lines:
        BatchLine.objects.create(
            batch=batch,
            part_type=refs["part"],
            quantity=Decimal(qty),
            unit_cost_currency=Decimal(unit_cost),
        )
    batch.status = status
    batch.save(update_fields=["status"])
    return batch


def _alloc(computed):
    return [row["allocated"] for row in computed["lines"]]


# --- Расчёт распределения (compute_landed_cost) ----------------------------


def test_landed_cost_by_value_proportional(refs):
    # Пример заказчика: товары 500 000 + доставка 80 000 + таможня 40 000.
    batch = _accepted_batch(
        refs, [("1", "300000"), ("1", "200000")], shipping_cost="80000", customs_cost="40000"
    )
    computed = compute_landed_cost(batch)
    a, b = computed["lines"]
    assert a["allocated"] == Decimal("72000.00")
    assert b["allocated"] == Decimal("48000.00")
    assert a["landed_total"] == Decimal("372000.00")
    assert b["landed_total"] == Decimal("248000.00")
    assert a["landed_unit"] == Decimal("372000.00")


def test_extra_cost_fully_distributed(refs):
    batch = _accepted_batch(
        refs, [("1", "300000"), ("1", "200000")], shipping_cost="80000", customs_cost="40000"
    )
    computed = compute_landed_cost(batch)
    assert sum(_alloc(computed)) == Decimal("120000.00")
    # Итог партии = товары + накладные.
    total_landed = sum(row["landed_total"] for row in computed["lines"])
    assert total_landed == Decimal("620000.00")


def test_rounding_remainder_goes_to_largest_line(refs):
    # 1.00 + 1.00 + 1.50 = 3.50; накладные 1.00. Наивные доли дают 1.01 —
    # копеечная разница добирается на крупнейшую строку (1.50).
    batch = _accepted_batch(
        refs, [("1", "1.00"), ("1", "1.00"), ("1", "1.50")], shipping_cost="1.00"
    )
    computed = compute_landed_cost(batch)
    allocs = _alloc(computed)
    assert sum(allocs) == Decimal("1.00")  # сходится до копейки
    # Крупнейшая строка (третья) поглощает разницу: 0.43 → 0.42.
    assert allocs[2] == Decimal("0.42")
    assert allocs[0] == Decimal("0.29")
    assert allocs[1] == Decimal("0.29")


def test_zero_value_falls_back_to_quantity(refs):
    # Бесплатные образцы (стоимость 0), но оплачена доставка 100 ₽.
    batch = _accepted_batch(
        refs, [("1", "0"), ("3", "0")], shipping_cost="100"
    )
    computed = compute_landed_cost(batch)
    assert computed["method"] == Batch.AllocationMethod.BY_QUANTITY
    assert _alloc(computed) == [Decimal("25.00"), Decimal("75.00")]
    assert sum(_alloc(computed)) == Decimal("100.00")


def test_no_extra_cost_keeps_base(refs):
    batch = _accepted_batch(refs, [("2", "100")], shipping_cost="0")
    computed = compute_landed_cost(batch)
    row = computed["lines"][0]
    assert row["allocated"] == Decimal("0.00")
    assert row["landed_total"] == Decimal("200.00")


def test_compute_without_lines_raises(refs):
    batch = _accepted_batch(refs, [], shipping_cost="100")
    with pytest.raises(LandedCostError):
        compute_landed_cost(batch)


# --- Фиксация (finalize_cost) ----------------------------------------------


def test_finalize_sets_state_and_freezes(make_user, refs):
    admin = make_user("admin", is_superuser=True)
    batch = _accepted_batch(
        refs, [("1", "300000"), ("1", "200000")], shipping_cost="80000", customs_cost="40000"
    )
    finalize_cost(batch, admin)
    batch.refresh_from_db()
    assert batch.status == Batch.Status.COST_CALCULATED
    assert batch.cost_finalized is True
    assert batch.cost_finalized_at is not None
    assert batch.cost_finalized_by == admin
    line = batch.lines.order_by("id").first()
    assert line.allocated_overhead_rub == Decimal("72000.00")
    assert line.landed_total_cost_rub == Decimal("372000.00")


def test_is_available_for_sale_only_after_finalize(make_user, refs):
    admin = make_user("admin", is_superuser=True)
    batch = _accepted_batch(refs, [("1", "100")], shipping_cost="10")
    assert batch.is_available_for_sale is False
    finalize_cost(batch, admin)
    batch.refresh_from_db()
    assert batch.is_available_for_sale is True


def test_cannot_finalize_non_accepted(make_user, refs):
    admin = make_user("admin", is_superuser=True)
    batch = _accepted_batch(refs, [("1", "100")], status=Batch.Status.DRAFT, shipping_cost="10")
    with pytest.raises(LandedCostError):
        finalize_cost(batch, admin)
    batch.refresh_from_db()
    assert batch.cost_finalized is False


def test_repeat_finalize_forbidden(make_user, refs):
    admin = make_user("admin", is_superuser=True)
    batch = _accepted_batch(refs, [("1", "100")], shipping_cost="10")
    finalize_cost(batch, admin)
    with pytest.raises(LandedCostError):
        finalize_cost(batch, admin)


# --- Экраны и права --------------------------------------------------------


def test_finalize_via_view(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    batch = _accepted_batch(refs, [("1", "100")], shipping_cost="10")
    resp = client.post(reverse("batch_cost_finalize", args=[batch.pk]))
    assert resp.status_code == 302
    batch.refresh_from_db()
    assert batch.cost_finalized is True
    assert batch.status == Batch.Status.COST_CALCULATED


def test_preview_does_not_persist(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    batch = _accepted_batch(refs, [("1", "100")], shipping_cost="10")
    resp = client.get(reverse("batch_cost_preview", args=[batch.pk]))
    assert resp.status_code == 200
    batch.refresh_from_db()
    assert batch.cost_finalized is False
    assert batch.lines.first().allocated_overhead_rub == Decimal("0")


def test_storekeeper_cannot_finalize(make_user, client, refs):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    batch = _accepted_batch(refs, [("1", "100")], shipping_cost="10")
    resp = client.post(reverse("batch_cost_finalize", args=[batch.pk]))
    assert resp.status_code == 403
    batch.refresh_from_db()
    assert batch.cost_finalized is False


def test_landed_sums_hidden_from_storekeeper(make_user, client, refs):
    admin = make_user("admin", is_superuser=True)
    make_user("sklad", role=roles.STOREKEEPER)
    batch = _accepted_batch(refs, [("1", "300000")], shipping_cost="72000")
    finalize_cost(batch, admin)  # landed_total = 372000

    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("batch_detail", args=[batch.pk])).content.decode()
    assert "372000" not in html  # landed-суммы скрыты

    client.logout()
    client.login(username="admin", password=PASSWORD)
    admin_html = client.get(reverse("batch_detail", args=[batch.pk])).content.decode()
    assert "372000" in admin_html


def test_lines_immutable_after_finalize(make_user, client, refs):
    admin = make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    batch = _accepted_batch(refs, [("2", "100")], shipping_cost="10")
    finalize_cost(batch, admin)
    line = batch.lines.first()

    # Редактирование строки заблокировано.
    resp = client.post(
        reverse("batch_line_edit", args=[line.pk]),
        {"part_type": refs["part"].pk, "quantity": "999", "unit_cost_currency": "1"},
    )
    assert resp.status_code == 302
    line.refresh_from_db()
    assert line.quantity == Decimal("2.000")

    # Удаление строки заблокировано.
    resp = client.post(reverse("batch_line_delete", args=[line.pk]))
    assert resp.status_code == 302
    assert BatchLine.objects.filter(pk=line.pk).exists()


def test_batch_costs_immutable_after_finalize(make_user, client, refs):
    admin = make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    batch = _accepted_batch(refs, [("1", "100")], shipping_cost="10")
    finalize_cost(batch, admin)
    resp = client.post(
        reverse("batch_edit", args=[batch.pk]),
        {
            "supplier": refs["sup"].pk,
            "currency": "RUB",
            "exchange_rate": "1",
            "goods_total": "0",
            "shipping_cost": "9999",
            "customs_cost": "0",
            "commission_cost": "0",
            "other_cost": "0",
        },
    )
    assert resp.status_code == 302
    batch.refresh_from_db()
    assert batch.shipping_cost == Decimal("10.00")  # не изменилось
