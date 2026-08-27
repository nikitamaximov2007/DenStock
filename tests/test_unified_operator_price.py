"""Одна цена для оператора на обычных экранах.

Оператору нужно ровно одно число, чтобы назвать цену клиенту. Раньше рядом
стояли три величины разного смысла - рекомендуемая цена, минимальная цена и
себестоимость лота, - и на складском экране самой заметной оказывалась
себестоимость. У детали, заведённой инвентаризацией без закупки, она равна
нулю, и экран выглядел так, будто деталь ничего не стоит.

Теперь везде подписано «Цена», и берётся она из одного места - канонической
цены детали. Себестоимость никуда не делась: она осталась под своим именем и
за тем же правом на закупочные цены, потому что это другая величина бизнеса.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts import roles
from apps.brp.models import BrpCatalogPart, BrpPricingSettings
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.catalog.services import create_manual_part
from apps.catalog_import.models import AftermarketCatalogPart
from apps.inventory.models import StockLot
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation, ValuationSettings

PASSWORD = "parol-12345"
PRICE = Decimal("20512")


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(
                username=username, password=PASSWORD
            )
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
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Аналоги")
    cell = StorageLocation.objects.create(
        name="Ячейка", code="S02-D01-C01", storage_allowed=True, is_active=True
    )
    rate = ValuationSettings.get()
    rate.current_usd_rate = Decimal("105")
    rate.save()
    markup = BrpPricingSettings.get()
    markup.brp_markup_percent = Decimal("40")
    markup.save()
    return {"sup": supplier, "cat": category, "cell": cell, "admin": admin}


def _part(env, *, name="ПОРШЕНЬ PROX", article="01.1395.100", price=PRICE):
    part = PartType.objects.create(
        name=name, category=env["cat"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=price,
    )
    PartNumber.objects.create(
        part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    return part


def _lot(env, part, *, quantity="2", unit_cost="0"):
    """Лот в ячейке. Нулевая себестоимость - это деталь, заведённая
    инвентаризацией: источника закупки у неё нет, и это законно."""
    batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["cell"], Decimal(quantity))
    receive_stock_lot(lot, by=env["admin"])
    return lot


def _login(client, env):
    client.force_login(env["admin"])
    return client


def _text(client, url):
    return client.get(url).content.decode().replace(" ", " ")


def _price_cell(body: str) -> str:
    """Содержимое ячейки строки «Цена» - без соседней строки себестоимости."""
    after = body.split("<th>Цена</th>", 1)[1]
    return after.split("<td>", 1)[1].split("</td>", 1)[0].strip()


def _shown(value: Decimal) -> str:
    """Число так, как его печатает money_int: разряды неразрывным пробелом."""
    return f"{int(value):,}".replace(",", " ")


# --- Главный случай: цена детали против себестоимости лота -------------------


def test_zero_lot_cost_does_not_become_the_operator_price(client, env):
    """Инвентаризованный аналог: цена 20512, себестоимость лота 0."""
    part = _part(env)
    lot = _lot(env, part, quantity="2", unit_cost="0")
    assert lot.landed_unit_cost_rub == Decimal("0.00")
    _login(client, env)

    for url in (reverse("lot_list"), reverse("lot_detail", args=[lot.pk])):
        body = _text(client, url)
        assert "Цена" in body, url
        assert _shown(PRICE) in body, url

    # Себестоимость в базе осталась нулём: экран её не переписывал.
    lot.refresh_from_db()
    assert lot.landed_unit_cost_rub == Decimal("0.00")
    assert StockLot.objects.get(pk=lot.pk).landed_unit_cost_rub == Decimal("0.00")


def test_a_real_lot_cost_is_not_shown_as_the_operator_price(client, env):
    """Себестоимость 8000 при цене 20512: оператор видит цену, не себестоимость."""
    part = _part(env)
    lot = _lot(env, part, quantity="2", unit_cost="8000")
    assert lot.landed_unit_cost_rub == Decimal("8000.00")
    _login(client, env)

    cell = _price_cell(_text(client, reverse("lot_detail", args=[lot.pk])))
    assert _shown(PRICE) in cell
    assert "8 000" not in cell  # себестоимость в ячейку цены не попала

    lot.refresh_from_db()
    assert lot.landed_unit_cost_rub == Decimal("8000.00")  # данные не тронуты


def test_the_cost_column_stays_behind_the_purchase_cost_right(client, env, make_user):
    """Обычный кладовщик себестоимости не видит, а цену видит."""
    part = _part(env)
    _lot(env, part, quantity="2", unit_cost="8000")
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)

    body = _text(client, reverse("lot_list"))
    assert "Цена" in body
    assert _shown(PRICE) in body
    assert "Себестоимость" not in body
    assert "8 000" not in body


# --- Словарь экранов ---------------------------------------------------------


@pytest.mark.parametrize(
    "url_name, args",
    [("part_search", None), ("part_detail", "part"), ("part_list", None),
     ("lot_list", None), ("item_list", None)],
)
def test_normal_operator_screens_speak_of_one_price(client, env, url_name, args):
    from apps.inventory.services import create_part_items

    part = _part(env)
    if url_name == "item_list":
        # Список экземпляров пуст, пока экземпляров нет: тогда и шапки нет.
        # Экземпляры бывают только у поштучных деталей, лотов у них не бывает.
        part.tracking_mode = PartType.TrackingMode.SERIAL
        part.save(update_fields=["tracking_mode"])
        batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch, part_type=part,
            quantity=Decimal("1"), unit_cost_currency=Decimal("0"),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, env["admin"])
        line.refresh_from_db()
        create_part_items(line, 1)
    else:
        _lot(env, part)
    _login(client, env)
    url = reverse(url_name, args=[part.pk] if args == "part" else None)
    if url_name == "part_search":
        url += f"?q={part.numbers.first().value}"
    body = _text(client, url)

    assert "Цена" in body
    assert "Рекомендуемая цена" not in body
    assert "Минимальная цена" not in body
    assert "минимальная:" not in body


def test_search_shows_the_price_once(client, env):
    _part(env)
    _login(client, env)
    body = _text(client, reverse("part_search") + "?q=01.1395.100")
    assert f"Цена: {_shown(PRICE)}" in body
    assert body.count("Цена:") == 1


def test_part_detail_has_one_price_row(client, env):
    part = _part(env)
    _login(client, env)
    body = _text(client, reverse("part_detail", args=[part.pk]))
    assert "<th>Цена</th>" in body
    assert body.count("<th>Цена</th>") == 1
    assert "Минимальная цена" not in body


def test_the_same_part_shows_the_same_price_everywhere(client, env):
    part = _part(env)
    lot = _lot(env, part)
    _login(client, env)
    shown = _shown(PRICE)
    for url in (
        reverse("part_search") + "?q=01.1395.100",
        reverse("part_detail", args=[part.pk]),
        reverse("part_list"),
        reverse("lot_list"),
        reverse("lot_detail", args=[lot.pk]),
        reverse("actions_scan") + "?q=01.1395.100",
    ):
        assert shown in _text(client, url), url


# --- Пустая цена -------------------------------------------------------------


def test_a_part_without_a_price_shows_a_dash_not_a_zero(client, env):
    lot = _lot(env, _part(env, name="БЕЗ ЦЕНЫ", article="NO-PRICE-1", price=None),
               quantity="2", unit_cost="0")
    _login(client, env)

    cell = _price_cell(_text(client, reverse("lot_detail", args=[lot.pk])))
    assert "—" in cell
    assert "0" not in cell  # ноль вместо цены не подставляется

    search = _text(client, reverse("part_search") + "?q=NO-PRICE-1")
    assert "Цена: —" in search


# --- Разные виды деталей -----------------------------------------------------


def test_an_aftermarket_part_uses_its_stored_price(client, env):
    """Цена берётся из карточки детали, а не считается в шаблоне из долларов."""
    from apps.catalog.models import Manufacturer

    part = _part(env, name="PROX PISTON", article="PX-1")
    AftermarketCatalogPart.objects.create(
        source=AftermarketCatalogPart.SOURCE_DEALER_2023, part=part,
        manufacturer=Manufacturer.objects.create(name="PROX"),
        manufacturer_number="PX-1", normalized_manufacturer_number="PX1",
        supplier_sku="SKU-1", source_description="PROX PISTON",
        dealer_cost_usd=Decimal("139.54"),
    )
    _lot(env, part, quantity="2", unit_cost="0")
    _login(client, env)

    for url in (reverse("part_search") + "?q=PX-1", reverse("lot_list")):
        body = _text(client, url)
        assert _shown(PRICE) in body, url
        assert "139.54" not in body, url  # доллары поставщика на экран не идут


def test_a_brp_part_speaks_the_same_vocabulary(client, env):
    brp = BrpCatalogPart.objects.create(
        material_no="417224916", material_no_norm="417224916",
        part_desc="ROLLER PULLEY", wholesale_price_usd=Decimal("28.15"),
        retail_price_usd=Decimal("35.99"), is_current=True,
    )
    part = promote_to_warehouse(brp, by=env["admin"])
    _login(client, env)

    body = _text(client, reverse("part_detail", args=[part.pk]))
    assert "<th>Цена</th>" in body
    assert "Минимальная цена" not in body

    catalog = _text(client, reverse("brp_search") + "?q=417224916")
    assert "Розница BRP" not in catalog  # прежняя чистка каталога цела
    assert "35.99" not in catalog


def test_a_manual_part_speaks_the_same_vocabulary(client, env):
    part = create_manual_part(
        name="Пружина ручная", article="MANUAL-77", price=Decimal("450"),
        manufacturer_name="Мастерская",
    )
    _login(client, env)
    body = _text(client, reverse("part_detail", args=[part.pk]))
    assert "<th>Цена</th>" in body
    assert "450" in body


# --- Финансовые экраны не тронуты --------------------------------------------


def test_financial_screens_keep_saying_cost(client, env):
    """Себестоимость остаётся себестоимостью там, где она и есть учётная."""
    part = _part(env)
    lot = _lot(env, part, quantity="2", unit_cost="8000")
    _login(client, env)

    body = _text(client, reverse("lot_detail", args=[lot.pk]))
    assert "Себестоимость за ед." in body
    assert "8 000" in body  # настоящая себестоимость видна и подписана верно


def test_rendering_never_writes_to_the_database(client, env):
    part = _part(env)
    lot = _lot(env, part, quantity="2", unit_cost="0")
    before = (
        StockLot.objects.count(),
        StockLot.objects.get(pk=lot.pk).landed_unit_cost_rub,
        StockLot.objects.get(pk=lot.pk).quantity,
        PartType.objects.get(pk=part.pk).recommended_price,
    )
    _login(client, env)
    for url in (reverse("lot_list"), reverse("lot_detail", args=[lot.pk]),
                reverse("part_detail", args=[part.pk]),
                reverse("part_search") + "?q=01.1395.100"):
        client.get(url)
    assert (
        StockLot.objects.count(),
        StockLot.objects.get(pk=lot.pk).landed_unit_cost_rub,
        StockLot.objects.get(pk=lot.pk).quantity,
        PartType.objects.get(pk=part.pk).recommended_price,
    ) == before


# --- Запросы -----------------------------------------------------------------


def test_the_price_column_adds_no_query_per_row(client, env):
    """Цена лежит на самой детали, а `select_related` уже есть в списке."""
    _login(client, env)
    url = reverse("lot_list")

    for index in range(3):
        _lot(env, _part(env, name=f"Д{index}", article=f"ART-{index}"))
    with CaptureQueriesContext(connection) as small:
        assert client.get(url).status_code == 200
    few = len(small.captured_queries)

    for index in range(3, 25):
        _lot(env, _part(env, name=f"Д{index}", article=f"ART-{index}"))
    with CaptureQueriesContext(connection) as many:
        assert client.get(url).status_code == 200
    assert len(many.captured_queries) == few
