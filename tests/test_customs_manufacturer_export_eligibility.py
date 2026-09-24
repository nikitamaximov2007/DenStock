"""BRP/PRO-X manufacturer classification, export eligibility and chronology.

Три дефекта, которые тут закрываются:

1. ``PartCustomsInfo.manufacturer`` раньше был по умолчанию «BRP» (значение
   поля модели). Любая ручная деталь, чью таможенную карточку хоть кто-то
   открыл или сохранил без явного выбора производителя, молча становилась
   «BRP». Ручное создание НЕ означает BRP - производитель должен оставаться
   пустым, пока не доказан (каталожной связью или явным выбором).

2. Очередь таможенного заказа (``apps.customs_orders``) собирала ВСЕ
   продажи/ремонты без единого фильтра по производителю. BRONCO/SPI/MOTUL и
   непроверенные ручные детали могли попасть в ту же BRP/PRO-X отправку, что
   и настоящий BRP. Допуск к отправке - явный список (BRP, PROX), а не
   побочный эффект производителя или факта ручного создания.

3. Строки «Экспорт в Excel для таможни» сортировались по артикулу/названию, а
   «История для таможенных заказов» - хронологически по дате операции. Один и
   тот же набор операций показывался в разном порядке. Порядок обязан
   совпадать: старые операции сверху, новые снизу.

Тесты используют реальные сервисные функции (``apply_system_customs_facts``,
``get_or_create_customs``), а не подставляют значение производителя руками -
иначе проверялась бы не система, а сама подстановка.
"""
import datetime
from decimal import Decimal

import pytest

from apps.actions.models import PartCustomsInfo
from apps.actions.services import (
    apply_system_customs_facts,
    get_or_create_customs,
    historical_customs_rows,
    is_brp_export_eligible,
    manual_part_name_ru,
    perform_action,
)
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.catalog.services import create_manual_part
from apps.customs_orders.models import CustomsOrder
from apps.customs_orders.services import customs_sources, eligible_customs_sources
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from tests.customs_support import legacy_customs_completion

PASSWORD = "parol-12345"
ApplicationArea = PartCustomsInfo.ApplicationArea


# --- Обстановка --------------------------------------------------------------


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, is_superuser=True):
        return django_user_model.objects.create_superuser(username=username, password=PASSWORD)

    return _make


@pytest.fixture
def env(db, make_user):
    admin = make_user("boss")
    supplier, _ = Supplier.objects.get_or_create(name="ООО Поставка")
    location, _ = StorageLocation.objects.get_or_create(
        code="S01-D01-C01",
        defaults={"name": "Ячейка", "storage_allowed": True, "is_active": True},
    )
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    return {"admin": admin, "sup": supplier, "loc": location, "cat": category}


def _receive(env, part, quantity="10"):
    batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["loc"], Decimal(quantity))
    receive_stock_lot(lot, by=env["admin"])


def _manual_part(env, *, name, article, manufacturer_name=""):
    """Деталь ровно тем путём, каким её заводит склад: apps.catalog.services."""
    part = create_manual_part(
        name=name, article=article, manufacturer_name=manufacturer_name, price="1000",
    )
    _receive(env, part)
    return part


def _catalog_part(env, *, name, article):
    """Обычная каталожная (не ручная) карточка - для тестов «не выдумано»."""
    part = PartType.objects.create(
        name=name, category=env["cat"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("1000"),
    )
    PartNumber.objects.create(part=part, value=article, kind=PartNumber.Kind.OEM, is_primary=True)
    _receive(env, part)
    return part


def _declare_customs(part, *, by):
    """Пройти карточку так же, как форма: производитель system-derived.

    Ничего не подставляем руками - ``apply_system_customs_facts`` берёт
    производителя из доказанной связи/явного выбора (``manufacturer_display``),
    ровно как ``actions_customs_edit`` при настоящем сохранении.
    """
    customs = get_or_create_customs(part)
    customs.customs_name_ru = "НАЗВАНИЕ"
    customs.customs_name_ru_confirmed = True
    customs.gross_weight_kg = Decimal("0.250")
    customs.net_weight_kg = Decimal("0.200")
    customs.application_area = ApplicationArea.SNOWMOBILE
    apply_system_customs_facts(customs)
    customs.updated_by = by
    customs.save()
    customs.refresh_from_db()
    return customs


def _sell(env, part, *, quantity="1", number, at=None):
    """Продажа. Вес/область применения дозаполняются на момент проведения,
    как и в остальных таможенных тестах (см. tests/customs_support.py) - это
    условие проводки, а не факт, который тест собирается проверять."""
    with legacy_customs_completion(part):
        action = perform_action(
            part=part, location=env["loc"], action_type="sale", quantity=quantity,
            customer_comment="Клиент", scanned_number=number, by=env["admin"],
        )
    if at is not None:
        Sale.objects.filter(pk=action.sale_id).update(sold_at=at)
    return action


def _at(day, hour=12):
    return datetime.datetime(2026, 9, day, hour, tzinfo=datetime.UTC)


def _row_for(rows, number):
    found = [r for r in rows if r["number"] == number]
    assert len(found) == 1, [r["number"] for r in rows]
    return found[0]


# --- MANUFACTURER: 6-12 -------------------------------------------------------


def test_manual_part_is_not_automatically_brp(env):
    """Открытие карточки ручной детали не должно проставлять BRP."""
    part = _manual_part(env, name="ГИЛЬЗА МАСЛОНАСОСА", article="10F")
    customs = get_or_create_customs(part)  # то же, что делает GET страницы правки
    assert customs.manufacturer == ""
    assert not is_brp_export_eligible(customs.manufacturer)


def test_manual_part_stays_unknown_after_a_real_save_without_a_manufacturer(env):
    """Ручная деталь без выбранного производителя не становится BRP и после
    настоящего сохранения формы - «unset» остаётся законным состоянием."""
    part = _manual_part(env, name="ПРОКЛАДКА", article="11G2")
    customs = _declare_customs(part, by=env["admin"])
    assert customs.manufacturer == ""
    assert not is_brp_export_eligible(customs.manufacturer)


@pytest.mark.parametrize(
    "brand, eligible",
    [("BRONCO", False), ("SPI", False), ("MOTUL", False), ("PROX", True)],
)
def test_manual_part_keeps_its_declared_brand(env, brand, eligible):
    """Явно выбранный производитель ручной детали сохраняется как есть -
    BRONCO/SPI/MOTUL остаются собой и не входят в BRP/PRO-X выгрузку; PRO-X
    остаётся PRO-X (не превращается в BRP) и допуск у него положительный."""
    part = _manual_part(
        env, name=f"ДЕТАЛЬ {brand}", article=f"ART-{brand}", manufacturer_name=brand,
    )
    customs = _declare_customs(part, by=env["admin"])
    assert customs.manufacturer == brand
    assert is_brp_export_eligible(customs.manufacturer) is eligible


def test_real_brp_catalog_link_remains_brp(env):
    """Каталожная BRP-позиция остаётся BRP и без единого касания формы."""
    brp = BrpCatalogPart.objects.create(
        material_no="219800345", part_desc="BELT DRIVE",
        wholesale_price_usd=Decimal("28.15"),
    )
    part = promote_to_warehouse(brp, by=env["admin"])
    _receive(env, part)
    customs = get_or_create_customs(part)  # предзаполнение по доказанной связи
    assert customs.manufacturer == "BRP"
    assert is_brp_export_eligible(customs.manufacturer)


def test_unknown_manual_part_is_excluded_from_brp_export(env):
    part = _manual_part(env, name="ЗАГАДОЧНАЯ ДЕТАЛЬ", article="12G4")
    _declare_customs(part, by=env["admin"])
    _sell(env, part, number="12G4")
    articles = {row["number"] for row in eligible_customs_sources()}
    assert "12G4" not in articles


# --- EXPORT ELIGIBILITY: 13-18 (таможенный заказ BRP/PRO-X) ------------------


def test_brp_and_pro_x_are_eligible_bronco_spi_motul_are_not(env):
    parts = {
        "BRP": _manual_part(env, name="BRP ДЕТАЛЬ", article="A-BRP", manufacturer_name="BRP"),
        "PROX": _manual_part(env, name="PROX ДЕТАЛЬ", article="A-PROX", manufacturer_name="PROX"),
        "BRONCO": _manual_part(
            env, name="BRONCO ДЕТАЛЬ", article="A-BRONCO", manufacturer_name="BRONCO"
        ),
        "SPI": _manual_part(env, name="SPI ДЕТАЛЬ", article="A-SPI", manufacturer_name="SPI"),
        "MOTUL": _manual_part(
            env, name="MOTUL ДЕТАЛЬ", article="A-MOTUL", manufacturer_name="MOTUL"
        ),
    }
    for article, part in zip(
        ("A-BRP", "A-PROX", "A-BRONCO", "A-SPI", "A-MOTUL"), parts.values(), strict=True
    ):
        _declare_customs(part, by=env["admin"])
        _sell(env, part, number=article)

    eligible_articles = {row["number"] for row in eligible_customs_sources()}
    assert eligible_articles == {"A-BRP", "A-PROX"}

    all_history_articles = {row["number"] for row in customs_sources()}
    # «История» показывает всё - допуск к отправке не то же самое, что
    # видимость очереди.
    assert all_history_articles == {"A-BRP", "A-PROX", "A-BRONCO", "A-SPI", "A-MOTUL"}


# --- ORDERING: 1-5, включая фикстуру из раздела 11 задания -------------------


def test_history_is_chronological_oldest_to_newest(env):
    a = _manual_part(env, name="A", article="ORDER-A", manufacturer_name="BRP")
    b = _manual_part(env, name="B", article="ORDER-B", manufacturer_name="BRP")
    c = _manual_part(env, name="C", article="ORDER-C", manufacturer_name="BRP")
    for part in (a, b, c):
        _declare_customs(part, by=env["admin"])
    _sell(env, c, number="ORDER-C", at=_at(17))
    _sell(env, a, number="ORDER-A", at=_at(9))
    _sell(env, b, number="ORDER-B", at=_at(10))

    assert [row["number"] for row in customs_sources()] == ["ORDER-A", "ORDER-B", "ORDER-C"]


def test_excel_export_is_chronological_not_alphabetical(env):
    """AT-08776 (BRONCO) чуть свет не упал в конец из-за сортировки по
    артикулу; РОЛИК ШКИВА (позже по дате) не обязан уходить выше него."""
    at_08776 = _manual_part(
        env, name="BRONCO TIE ТЯГА END", article="AT-08776", manufacturer_name="BRONCO",
    )
    roller = _manual_part(env, name="РОЛИК ШКИВА", article="AA-ROLLER", manufacturer_name="BRP")
    for part in (at_08776, roller):
        _declare_customs(part, by=env["admin"])
    _sell(env, at_08776, number="AT-08776", at=_at(10, 15))
    _sell(env, roller, number="AA-ROLLER", at=_at(17, 12))

    numbers = [row["number"] for row in historical_customs_rows()]
    # Алфавитный порядок поставил бы AA-ROLLER выше AT-08776 - здесь наоборот,
    # потому что AT-08776 продан раньше.
    assert numbers == ["AT-08776", "AA-ROLLER"]


def test_repeated_article_operations_preserve_chronology_and_are_not_deduplicated(env):
    part = _manual_part(env, name="ПОВТОР", article="REPEAT-1", manufacturer_name="BRP")
    _declare_customs(part, by=env["admin"])
    _sell(env, part, quantity="1", number="REPEAT-1", at=_at(9))
    other = _manual_part(env, name="МЕЖДУ", article="BETWEEN-1", manufacturer_name="BRP")
    _declare_customs(other, by=env["admin"])
    _sell(env, other, number="BETWEEN-1", at=_at(10))
    _sell(env, part, quantity="1", number="REPEAT-1", at=_at(12))

    rows = historical_customs_rows()
    # Один и тот же артикул продан дважды: строка объединена (та же деталь,
    # та же версия таможенных данных), но её количество - сумма обеих продаж,
    # а не одна из них молча потерялась.
    repeat_row = _row_for(rows, "REPEAT-1")
    assert repeat_row["quantity"] == Decimal("2")
    assert [row["number"] for row in rows] == ["REPEAT-1", "BETWEEN-1"]


def test_same_timestamp_ties_break_deterministically(env):
    """Тай-брейк для одинаковых меток времени - по id канонической строки
    (SaleLine), а не по случайному порядку выборки: строка, проведённая
    раньше, остаётся выше, даже если её дата операции совпала с другой."""
    same_moment = _at(10)
    first = _manual_part(env, name="ПЕРВАЯ", article="TIE-1", manufacturer_name="BRP")
    second = _manual_part(env, name="ВТОРАЯ", article="TIE-2", manufacturer_name="BRP")
    for part in (first, second):
        _declare_customs(part, by=env["admin"])
    action_first = _sell(env, first, number="TIE-1", at=same_moment)
    action_second = _sell(env, second, number="TIE-2", at=same_moment)
    assert action_first.sale_id < action_second.sale_id

    order_a = [row["number"] for row in historical_customs_rows()]
    order_b = [row["number"] for row in historical_customs_rows()]
    assert order_a == order_b == ["TIE-1", "TIE-2"]  # стабильно между вызовами


def test_brp_pro_x_shipment_keeps_relative_order_after_excluding_other_brands(env):
    """Точная регрессия из раздела 11 задания: интерливинг операций разных
    производителей. История видит всё; допущенная к отправке выгрузка -
    только BRP/PRO-X, но в том же относительном хронологическом порядке."""
    specs = [
        (9, "OLD-BRP", "BRP", True),
        (10, "BRONCO-1", "BRONCO", False),
        (10, "BRP-1", "BRP", True),
        (12, "SPI-1", "SPI", False),
        (14, "PROX-1", "PROX", True),
        (15, "MOTUL-1", "MOTUL", False),
        (17, "NEW-BRP", "BRP", True),
    ]
    parts = {}
    for _day, article, brand, _eligible in specs:
        part = _manual_part(
            env, name=f"{brand} {article}", article=article, manufacturer_name=brand,
        )
        _declare_customs(part, by=env["admin"])
        parts[article] = part
    for index, (day, article, _brand, _eligible) in enumerate(specs):
        _sell(env, parts[article], number=article, at=_at(day, hour=10 + index))

    history_articles = [row["number"] for row in customs_sources()]
    assert history_articles == [article for _day, article, _brand, _eligible in specs]

    shipment_articles = [row["number"] for row in eligible_customs_sources()]
    expected = [article for _day, article, _brand, eligible in specs if eligible]
    assert shipment_articles == expected == ["OLD-BRP", "BRP-1", "PROX-1", "NEW-BRP"]


# --- NAMES: 19-21 -------------------------------------------------------------


def test_manual_russian_name_flows_into_export_uppercased(env):
    part = _manual_part(env, name="Гильза маслонасоса", article="10F", manufacturer_name="BRP")
    _sell(env, part, number="10F")  # ни разу не открывали таможенную карточку

    row = _row_for(historical_customs_rows(), "10F")
    assert row["name_ru"] == "ГИЛЬЗА МАСЛОНАСОСА"
    assert row["name_ru_confirmed"] is False  # подстановка - не подтверждение


def test_manual_russian_name_helper_ignores_catalog_linked_parts(env):
    """Название каталожной карточки может быть английской строкой прайса -
    его нельзя подставлять как русское таможенное название."""
    part = _catalog_part(env, name="DRIVE BELT", article="700100700")
    assert manual_part_name_ru(part) == ""


def test_missing_russian_name_is_not_fabricated_for_non_manual_parts(env):
    part = _catalog_part(env, name="НАЗВАНИЕ ИЗ КАТАЛОГА", article="700100700")
    _sell(env, part, number="700100700")
    row = _row_for(historical_customs_rows(), "700100700")
    assert row["name_ru"] == ""


# --- SAFETY: 22-25 -------------------------------------------------------------


def test_reading_exports_and_history_does_not_mutate_stock_sales_or_prices(env):
    part = _manual_part(env, name="БЕЗОПАСНОСТЬ", article="SAFE-1", manufacturer_name="BRP")
    _declare_customs(part, by=env["admin"])
    _sell(env, part, number="SAFE-1")
    part.refresh_from_db()

    before = (
        StockMovement.objects.count(),
        Sale.objects.count(),
        RepairOrder.objects.count(),
        CustomsOrder.objects.count(),
        part.recommended_price,
    )
    list(customs_sources())
    list(eligible_customs_sources())
    list(historical_customs_rows())
    part.refresh_from_db()
    after = (
        StockMovement.objects.count(),
        Sale.objects.count(),
        RepairOrder.objects.count(),
        CustomsOrder.objects.count(),
        part.recommended_price,
    )
    assert before == after
