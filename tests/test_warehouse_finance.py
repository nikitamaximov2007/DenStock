"""Финансовая оценка склада: закупочная стоимость, оценка продажи, прибыль.

Гарантии: закупка BRP — retail USD (база до наценки) x курс настройки (105);
закупка Polaris — ТОЛЬКО «ОПТОВАЯ»; replacement/superseded — только источник
цены, identity не меняется; позиции без закупочной цены не считаются нулём,
а идут в отдельный счётчик; BRP и Polaris не смешиваются; продажа/отмена/
довнесение найденной детали корректно двигают показатели; расчёт read-only
и не делает запрос цены на каждую строку остатка.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.services import cancel_warehouse_action, perform_action
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse as promote_brp
from apps.catalog.models import Category, PartType, Unit
from apps.catalog.services import update_current_price_settings
from apps.inventory.models import StockLot
from apps.inventory.services import (
    add_found_stock,
    create_stock_lot,
    receive_stock_lot,
    write_off_stock_lot_quantity,
)
from apps.polaris.models import PolarisCatalogPart
from apps.polaris.services import promote_to_warehouse as promote_polaris
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.reports.statistics import get_statistics, resolve_stats_period
from apps.reports.warehouse_finance import _category_identity, get_warehouse_valuation
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation, ValuationSettings

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
def admin(make_user):
    return make_user("admin", is_superuser=True)


@pytest.fixture
def env(db, admin):
    sup = Supplier.objects.create(name="Стартовый ввод")
    loc = StorageLocation.objects.create(
        name="C04", code="S04-L03-D01-C04", storage_allowed=True, is_active=True
    )
    return {"sup": sup, "loc": loc, "admin": admin}


def _stock(part, location, qty, sup, admin):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(str(qty)),
        unit_cost_currency=Decimal("1"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


def _brp_part(env, *, material="219800345", retail="10", replacement="", desc="BELT"):
    brp = BrpCatalogPart.objects.create(
        material_no=material, part_desc=desc,
        retail_price_usd=Decimal(retail), replacement_no_1=replacement,
    )
    return promote_brp(brp, by=env["admin"]), brp


def _polaris_part(env, *, number="3610075", wholesale, retail="20", desc="SEAL"):
    polaris = PolarisCatalogPart.objects.create(
        part_number=number, part_name=desc,
        wholesale_price_usd=Decimal(wholesale) if wholesale is not None else None,
        retail_price_usd=Decimal(retail) if retail is not None else None,
    )
    return promote_polaris(polaris, by=env["admin"]), polaris


def _manual_part(*, name, category, price):
    return PartType.objects.create(
        name=name,
        category=category,
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=price,
    )


# --- 1. BRP: базовый расчёт -----------------------------------------------------------


def test_brp_purchase_sale_and_profit(env):
    part, _brp = _brp_part(env, retail="10")  # клиентская 10*105*1.4 = 1470
    _stock(part, env["loc"], 3, env["sup"], env["admin"])
    v = get_warehouse_valuation()
    assert v.purchase_cost == Decimal("3150.00")  # 3 x 10 x 105
    assert v.sale_value == Decimal("4410.00")  # 3 x 1470 (существующая клиентская цена)
    assert v.potential_profit == Decimal("1260.00")  # разница
    assert v.unpriced_positions == 0
    assert v.usd_rate == Decimal("105")


def test_brp_category_sale_value_matches_total(env):
    part, _brp = _brp_part(env, retail="10")
    _stock(part, env["loc"], 3, env["sup"], env["admin"])

    stats = get_statistics(resolve_stats_period({}))

    assert [(row.name, row.value) for row in stats.value_by_category] == [
        ("BRP", stats.valuation.sale_value)
    ]
    assert stats.value_by_category[0].quantity == Decimal("3")


def test_multiple_lots_of_same_brp_part_are_summed_once(env):
    part, _brp = _brp_part(env, retail="10")
    _stock(part, env["loc"], Decimal("1.25"), env["sup"], env["admin"])
    _stock(part, env["loc"], Decimal("2.75"), env["sup"], env["admin"])

    valuation = get_warehouse_valuation()

    assert valuation.physical_units == Decimal("4.00")
    assert valuation.sale_by_category[0].quantity == Decimal("4.00")
    assert valuation.sale_by_category[0].value == Decimal("5880.00")


def test_multiple_parts_in_one_category_are_summed(env):
    first, _ = _brp_part(env, material="700000001", retail="10")
    second, _ = _brp_part(env, material="700000002", retail="20")
    _stock(first, env["loc"], 1, env["sup"], env["admin"])
    _stock(second, env["loc"], 2, env["sup"], env["admin"])

    valuation = get_warehouse_valuation()

    assert len(valuation.sale_by_category) == 1
    assert valuation.sale_by_category[0].quantity == Decimal("3")
    assert valuation.sale_by_category[0].value == Decimal("7350.00")


def test_uncategorized_identity_has_explicit_label():
    class PartWithoutCategory:
        category_id = None
        category = None

    assert _category_identity(PartWithoutCategory()) == (None, "Без категории")


# --- 2. BRP replacement как источник цены ----------------------------------------------


def test_brp_replacement_price_source_keeps_identity(env):
    # Exact 420931285 с ценой 0; replacement 420931284 имеет цену 4 USD.
    BrpCatalogPart.objects.create(
        material_no="420931284", part_desc="OLD SEAL", retail_price_usd=Decimal("4"),
        replacement_no_1="420931285",
    )
    part, brp = _brp_part(env, material="420931285", retail="0", desc="OIL SEAL")
    _stock(part, env["loc"], 2, env["sup"], env["admin"])
    v = get_warehouse_valuation()
    assert v.purchase_cost == Decimal("840.00")  # 2 x 4 x 105 (цена от replacement)
    assert v.sale_value == Decimal("1176.00")  # 2 x (4*105*1.4=588)
    assert v.unpriced_positions == 0
    # Identity не тронута: каталожная запись exact-номера не изменилась.
    brp.refresh_from_db()
    assert brp.material_no == "420931285"
    assert brp.retail_price_usd == Decimal("0")


# --- 3-4. Polaris: только «ОПТОВАЯ»; без оптовой -> счётчик ----------------------------


def test_polaris_uses_wholesale_only(env):
    part, _pol = _polaris_part(env, wholesale="6", retail="20")
    _stock(part, env["loc"], 4, env["sup"], env["admin"])
    v = get_warehouse_valuation()
    assert v.purchase_cost == Decimal("2520.00")  # 4 x 6 x 105 (НЕ retail 20)
    # Оценка продажи — существующая клиентская цена Polaris (retail-база):
    # 20 * 105 * 1.4 = 2940; 4 шт = 11760.
    assert v.sale_value == Decimal("11760.00")
    assert v.unpriced_positions == 0


def test_polaris_without_wholesale_goes_to_unpriced(env):
    part, _pol = _polaris_part(env, number="9999999", wholesale=None, retail="20")
    _stock(part, env["loc"], 5, env["sup"], env["admin"])
    v = get_warehouse_valuation()
    assert v.purchase_cost == Decimal("0.00")  # не фиктивные 0 за 5 единиц, а исключение
    assert v.unpriced_positions == 1
    assert v.unpriced_units == Decimal("5")
    assert v.sale_value == Decimal("14700.00")  # продажная оценка всё равно считается


def test_polaris_superseded_wholesale_source(env):
    # Exact без оптовой; superseded-связанная запись имеет оптовую 8 USD.
    PolarisCatalogPart.objects.create(
        part_number="1111111", part_name="OLD", wholesale_price_usd=Decimal("8"),
        retail_price_usd=Decimal("15"), superseded_number="2222222",
    )
    part, pol = _polaris_part(env, number="2222222", wholesale="0", retail="20")
    _stock(part, env["loc"], 2, env["sup"], env["admin"])
    v = get_warehouse_valuation()
    assert v.purchase_cost == Decimal("1680.00")  # 2 x 8 x 105 — оптовая от superseded
    assert v.unpriced_positions == 0
    pol.refresh_from_db()
    assert pol.part_number == "2222222"  # identity не подменена


# --- 5. Смешанный склад -----------------------------------------------------------------


def test_mixed_brp_polaris_same_number_not_merged(env):
    brp_part, _b = _brp_part(env, material="5555555", retail="10")
    pol_part, _p = _polaris_part(env, number="5555555", wholesale="6", retail="20")
    _stock(brp_part, env["loc"], 1, env["sup"], env["admin"])
    _stock(pol_part, env["loc"], 1, env["sup"], env["admin"])
    v = get_warehouse_valuation()
    # BRP: 10x105=1050; Polaris: 6x105=630. Не смешаны, итог = сумма.
    assert v.purchase_cost == Decimal("1680.00")
    assert v.sale_value == Decimal("1470.00") + Decimal("2940.00")
    assert {row.name: row.value for row in v.sale_by_category} == {
        "BRP": Decimal("1470.00"),
        "POLARIS": Decimal("2940.00"),
    }
    assert sum((row.value for row in v.sale_by_category), Decimal("0")) == v.sale_value
    assert v.unpriced_positions == 0


# --- 6-7. Продажа и отмена продажи -------------------------------------------------------


def test_sale_and_cancel_move_all_three_numbers(env):
    part, _brp = _brp_part(env, retail="10")
    _stock(part, env["loc"], 3, env["sup"], env["admin"])
    before = get_warehouse_valuation()
    action = perform_action(
        part=part, location=env["loc"], action_type="sale", quantity="1",
        customer_comment="Иванов", by=env["admin"],
    )
    after_sale = get_warehouse_valuation()
    assert after_sale.purchase_cost == Decimal("2100.00")  # 2 x 10 x 105
    assert after_sale.sale_value == Decimal("2940.00")
    assert after_sale.potential_profit == Decimal("840.00")
    # Отмена возвращает остаток — показатели восстанавливаются.
    cancel_warehouse_action(action, by=env["admin"], reason="Ошибка")
    restored = get_warehouse_valuation()
    assert restored.purchase_cost == before.purchase_cost
    assert restored.sale_value == before.sale_value
    assert restored.potential_profit == before.potential_profit


def test_completed_sale_is_excluded_from_category_and_total(env):
    part, _brp = _brp_part(env, retail="10")
    _stock(part, env["loc"], 3, env["sup"], env["admin"])

    perform_action(
        part=part,
        location=env["loc"],
        action_type="sale",
        quantity="1",
        customer_comment="Иванов",
        by=env["admin"],
    )
    valuation = get_warehouse_valuation()

    assert valuation.physical_units == Decimal("2")
    assert valuation.sale_value == Decimal("2940.00")
    assert valuation.sale_by_category[0].value == valuation.sale_value


def test_written_off_quantity_is_excluded_from_category_and_total(env):
    part, _brp = _brp_part(env, retail="10")
    lot = _stock(part, env["loc"], 3, env["sup"], env["admin"])

    write_off_stock_lot_quantity(lot, Decimal("1"), by=env["admin"])
    valuation = get_warehouse_valuation()

    assert valuation.physical_units == Decimal("2")
    assert valuation.sale_value == Decimal("2940.00")
    assert valuation.sale_by_category[0].value == valuation.sale_value


def test_active_reservation_keeps_physical_sale_value(env):
    part, _brp = _brp_part(env, retail="10")
    lot = _stock(part, env["loc"], 3, env["sup"], env["admin"])
    before = get_warehouse_valuation()
    reservation = create_reservation(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=env["admin"])
    activate_reservation(reservation, by=env["admin"])

    after = get_warehouse_valuation()

    assert after.sale_value == before.sale_value
    assert after.sale_by_category == before.sale_by_category


def test_quarantine_keeps_physical_sale_value(env):
    part, _brp = _brp_part(env, retail="10")
    lot = _stock(part, env["loc"], 3, env["sup"], env["admin"])
    before = get_warehouse_valuation()
    lot.status = StockLot.Status.QUARANTINE
    lot.save(update_fields=["status", "updated_at"])

    after = get_warehouse_valuation()

    assert after.sale_value == before.sale_value
    assert after.sale_by_category == before.sale_by_category


# --- 8. Добавление найденной детали ------------------------------------------------------


def test_found_addition_increases_all_three(env):
    part, _brp = _brp_part(env, retail="10")
    _stock(part, env["loc"], 3, env["sup"], env["admin"])
    before = get_warehouse_valuation()
    add_found_stock(part, env["loc"], by=env["admin"])
    after = get_warehouse_valuation()
    assert after.purchase_cost - before.purchase_cost == Decimal("1050.00")  # +1 x 10 x 105
    assert after.sale_value - before.sale_value == Decimal("1470.00")
    assert after.potential_profit - before.potential_profit == Decimal("420.00")


# --- 9. Позиции без закупочной цены в UI --------------------------------------------------


def test_unpriced_positions_do_not_break_dashboard(client, make_user, env):
    part, _pol = _polaris_part(env, number="8888888", wholesale=None, retail="20")
    _stock(part, env["loc"], 2, env["sup"], env["admin"])
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("statistics_dashboard")).content.decode()
    assert "Закупочная стоимость склада" in html
    assert "Оценка склада по цене продажи" in html
    assert "Потенциальная прибыль" in html
    assert "Без закупочной цены: 1 позиций" in html
    assert "не включены в закупочную стоимость" in html  # пояснение о неполноте
    assert "Как считаются показатели?" in html
    assert "105" in html  # курс из настройки, не хардкод шаблона
    assert "—" not in html


def test_missing_sale_price_is_visible_and_does_not_break_invariant(env):
    category = Category.objects.create(name="Без цены")
    part = _manual_part(name="Без клиентской цены", category=category, price=None)
    _stock(part, env["loc"], 2, env["sup"], env["admin"])

    valuation = get_warehouse_valuation()

    assert valuation.sale_unpriced_positions == 1
    assert valuation.sale_unpriced_units == Decimal("2")
    assert valuation.sale_by_category[0].quantity == Decimal("2")
    assert valuation.sale_by_category[0].value == Decimal("0.00")
    assert sum((row.value for row in valuation.sale_by_category), Decimal("0")) == (
        valuation.sale_value
    )


def test_missing_sale_price_notice_is_rendered(client, make_user, env):
    category = Category.objects.create(name="Без цены")
    part = _manual_part(name="Без клиентской цены", category=category, price=None)
    _stock(part, env["loc"], 2, env["sup"], env["admin"])
    make_user("boss-no-price", is_superuser=True)
    client.login(username="boss-no-price", password=PASSWORD)

    html = client.get(reverse("statistics_dashboard")).content.decode()

    assert "Без клиентской цены: 1 позиций /" in html
    assert "Итого по категориям" in html


def test_fractional_quantity_uses_decimal_and_category_rounding(env):
    category = Category.objects.create(name="Дробные")
    part = _manual_part(name="Дробная деталь", category=category, price=Decimal("10.01"))
    _stock(part, env["loc"], Decimal("1.111"), env["sup"], env["admin"])

    valuation = get_warehouse_valuation()

    assert valuation.sale_by_category[0].value == Decimal("11.12")
    assert valuation.sale_value == Decimal("11.12")


def test_duplicate_category_names_are_grouped_by_category_pk(env):
    parent_a = Category.objects.create(name="Родитель A")
    parent_b = Category.objects.create(name="Родитель B")
    category_a = Category.objects.create(name="Одинаковая", parent=parent_a)
    category_b = Category.objects.create(name="Одинаковая", parent=parent_b)
    part_a = _manual_part(name="Первая", category=category_a, price=Decimal("100"))
    part_b = _manual_part(name="Вторая", category=category_b, price=Decimal("200"))
    _stock(part_a, env["loc"], 1, env["sup"], env["admin"])
    _stock(part_b, env["loc"], 1, env["sup"], env["admin"])

    valuation = get_warehouse_valuation()

    assert len(valuation.sale_by_category) == 2
    assert {row.category_id for row in valuation.sale_by_category} == {
        category_a.pk,
        category_b.pk,
    }
    assert valuation.sale_value == Decimal("300.00")


def test_manual_brp_price_is_used_for_total_and_category(env):
    brp = BrpCatalogPart.objects.create(
        material_no="700000099",
        part_desc="MANUAL",
        retail_price_usd=Decimal("10"),
    )
    part = promote_brp(brp, by=env["admin"], manual_price=Decimal("1234"))
    _stock(part, env["loc"], 2, env["sup"], env["admin"])

    valuation = get_warehouse_valuation()

    assert valuation.sale_value == Decimal("2468.00")
    assert valuation.sale_by_category[0].value == Decimal("2468.00")


def test_price_settings_change_total_and_categories_together(env):
    part, _brp = _brp_part(env, retail="10")
    _stock(part, env["loc"], 2, env["sup"], env["admin"])
    update_current_price_settings(
        current_usd_rate=Decimal("90.25"),
        brp_markup_percent=Decimal("40.25"),
        polaris_markup_percent=Decimal("35"),
        by=env["admin"],
    )

    valuation = get_warehouse_valuation()

    assert valuation.sale_value == Decimal("2532.00")
    assert valuation.sale_by_category[0].value == valuation.sale_value
    assert sum((row.value for row in valuation.sale_by_category), Decimal("0")) == (
        valuation.sale_value
    )


def test_custom_rate_setting_used(env):
    settings_row = ValuationSettings.get()
    settings_row.current_usd_rate = Decimal("90")
    settings_row.save(update_fields=["current_usd_rate"])
    part, _brp = _brp_part(env, retail="10")
    _stock(part, env["loc"], 1, env["sup"], env["admin"])
    v = get_warehouse_valuation()
    assert v.purchase_cost == Decimal("900.00")  # 1 x 10 x 90
    assert v.usd_rate == Decimal("90")


# --- 10. Производительность ---------------------------------------------------------------


def test_reasonable_query_count(env, django_assert_max_num_queries):
    # 10 видов деталей с остатком: запросов должно быть немного и БЕЗ
    # отдельного запроса цены на каждую строку остатка (fast-path при цене > 0).
    for i in range(10):
        part, _brp = _brp_part(env, material=f"70000000{i}", retail="10")
        _stock(part, env["loc"], 1, env["sup"], env["admin"])
    get_warehouse_valuation()  # прогрев настроек (get_or_create singleton)
    with django_assert_max_num_queries(8):
        valuation = get_warehouse_valuation()
    assert len(valuation.sale_by_category) == 1
    assert valuation.sale_by_category[0].value == valuation.sale_value


def test_replacement_sources_do_not_create_n_plus_one(env, django_assert_max_num_queries):
    for i in range(5):
        exact_number = f"71000000{i}"
        BrpCatalogPart.objects.create(
            material_no=f"71900000{i}",
            part_desc="OLD",
            retail_price_usd=Decimal("4"),
            replacement_no_1=exact_number,
        )
        brp_part, _ = _brp_part(env, material=exact_number, retail="0")
        _stock(brp_part, env["loc"], 1, env["sup"], env["admin"])

        old_number = f"361100{i}"
        exact_polaris_number = f"361200{i}"
        PolarisCatalogPart.objects.create(
            part_number=old_number,
            part_name="OLD",
            superseded_number=exact_polaris_number,
            wholesale_price_usd=Decimal("8"),
            retail_price_usd=Decimal("15"),
        )
        polaris_part, _ = _polaris_part(
            env,
            number=exact_polaris_number,
            wholesale="0",
            retail="0",
        )
        _stock(polaris_part, env["loc"], 1, env["sup"], env["admin"])

    get_warehouse_valuation()
    with django_assert_max_num_queries(8):
        valuation = get_warehouse_valuation()

    assert {row.name for row in valuation.sale_by_category} == {"BRP", "POLARIS"}
    assert sum((row.value for row in valuation.sale_by_category), Decimal("0")) == (
        valuation.sale_value
    )
