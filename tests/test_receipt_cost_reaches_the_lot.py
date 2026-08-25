"""Себестоимость проведённой приёмки обязана дойти до лота.

Разбор лота 21 показал, откуда берутся нулевые себестоимости в ремонтах: у
проведённой строки приёмки цена была 160, а у лота осталось 0. Исправление
самого лота - это разовая мера. Здесь закреплено то, что защищает от повторения.

Порядок в проведении приёмки не случаен: сначала считается себестоимость партии,
затем строка партии перечитывается из базы, и только потом из неё создаётся лот.
Уберите перечитывание или поменяйте шаги местами - и лот снова получит ноль,
причём молча: приёмка пройдёт, остаток появится, а цена выдачи в ремонт окажется
нулевой через несколько недель.
"""
from decimal import Decimal

import pytest

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockLot
from apps.receipts.models import Receipt, ReceiptLine
from apps.receipts.services import post_receipt
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="boss", password=PASSWORD)


@pytest.fixture
def scene(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    unit = Unit.objects.get(name="Штука")
    category = Category.objects.create(name="Двигатель")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S02-D03-C01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Поршень", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=part, value="250400101", kind=PartNumber.Kind.OEM, is_primary=True
    )
    return {"admin": admin, "supplier": supplier, "part": part, "location": location}


def _receipt(scene, *, quantity, unit_cost):
    receipt = Receipt.objects.create(supplier=scene["supplier"], created_by=scene["admin"])
    ReceiptLine.objects.create(
        receipt=receipt, part_type=scene["part"], location=scene["location"],
        quantity=Decimal(quantity), unit_cost_rub=Decimal(unit_cost),
    )
    return receipt


def test_a_posted_receipt_cost_lands_on_the_lot(scene):
    """Главный инвариант: положительная цена приёмки не превращается в ноль."""
    receipt = _receipt(scene, quantity="5", unit_cost="160")

    post_receipt(receipt, by=scene["admin"])

    lot = StockLot.objects.get(part_type=scene["part"])
    assert lot.landed_unit_cost_rub == Decimal("160.00")
    assert lot.landed_unit_cost_rub > 0, "лот получил ноль при положительной цене приёмки"


@pytest.mark.parametrize("unit_cost", ["0.01", "1", "160", "12345.67"])
def test_any_positive_receipt_cost_survives_to_the_lot(scene, unit_cost):
    receipt = _receipt(scene, quantity="2", unit_cost=unit_cost)

    post_receipt(receipt, by=scene["admin"])

    lot = StockLot.objects.get(part_type=scene["part"])
    assert lot.landed_unit_cost_rub == Decimal(unit_cost).quantize(Decimal("0.01"))


def test_the_lot_matches_the_batch_line_it_came_from(scene):
    """Лот копирует себестоимость строки партии, и они обязаны совпадать."""
    receipt = _receipt(scene, quantity="4", unit_cost="160")

    post_receipt(receipt, by=scene["admin"])

    line = ReceiptLine.objects.get(receipt=receipt)
    lot = StockLot.objects.get(part_type=scene["part"])
    line.batch_line.refresh_from_db()
    assert lot.landed_unit_cost_rub == line.batch_line.landed_unit_cost_rub
    assert lot.landed_unit_cost_rub == line.unit_cost_rub


def test_a_genuinely_free_receipt_still_gives_a_real_zero(scene):
    """Ноль остаётся законным, если его действительно ввели.

    Отличать надо не ноль от ненуля, а посчитанную величину от непосчитанной.
    """
    receipt = _receipt(scene, quantity="3", unit_cost="0")

    post_receipt(receipt, by=scene["admin"])

    lot = StockLot.objects.get(part_type=scene["part"])
    assert lot.landed_unit_cost_rub == Decimal("0.00")


def test_the_batch_line_is_reread_before_the_lot_is_made():
    """Проверка формы кода, а не поведения: порядок шагов здесь и есть защита.

    Себестоимость считается служебным вызовом, который меняет строку партии в
    базе. Объект в памяти после этого устаревает, и лот, созданный из него,
    унесёт прежний ноль. Перечитывание стоит между ними именно поэтому.
    """
    import inspect

    from apps.receipts import services

    source = inspect.getsource(services.post_receipt)
    finalize = source.index("finalize_cost(")
    reread = source.index("batch_line.refresh_from_db()")
    create = source.index("create_stock_lot(")
    assert finalize < reread < create, (
        "порядок шагов проведения изменился: себестоимость может не дойти до лота"
    )
