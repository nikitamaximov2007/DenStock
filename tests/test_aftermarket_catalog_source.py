"""Каталожная позиция не выдаёт себя за лежащую на складе.

Импорт каталога аналогов заводит настоящие карточки: имя, производитель,
артикул, SKU, цена клиента в рублях. Остатка у них нет - деталь никто не
принимал и ни в какую ячейку не клал. До этой правки поиск и пересчёт
называли такую карточку «На складе», и оператор шёл к пустой ячейке.

Подпись считается только по фактам. Ни лот, ни движение, ни остаток, ни
привязка к ячейке ради красивого статуса здесь не создаются - и тесты это
проверяют счётчиками.
"""
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from apps.brp.models import BrpCatalogPart, BrpPricingSettings
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.catalog_import.models import AftermarketCatalogPart, CatalogImportBatch
from apps.counting.models import InventoryCountingLine
from apps.counting.services import post_session, record_scan, start_session
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location
from apps.warehouse.models import ValuationSettings

PASSWORD = "parol-12345"
HEADERS = ["Manufacturer", "Item SKU", "Manufacturer Number", "Description", "Dlr Cost"]
ANALOG_ROW = ["PROX", "PX010", "01.1395.100", "PISTON KIT", "89.08"]


@pytest.fixture
def rates(db):
    valuation = ValuationSettings.get()
    valuation.current_usd_rate = Decimal("105")
    valuation.save()
    markup = BrpPricingSettings.get()
    markup.brp_markup_percent = Decimal("40")
    markup.save()
    return valuation


@pytest.fixture
def boss(db, django_user_model, client, rates):
    user = django_user_model.objects.create_superuser(username="boss", password=PASSWORD)
    client.login(username="boss", password=PASSWORD)
    client.user = user
    return client


def _book(rows):
    book = Workbook()
    book.remove(book.active)
    sheet = book.create_sheet("diorlight priceupdate")
    sheet.append(HEADERS)
    sheet.append([""] * len(HEADERS))
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def _import(client, rows=(ANALOG_ROW,)):
    """Завести карточки настоящим экраном импорта, а не руками в модели."""
    client.post(
        reverse("catalog_import_upload"),
        {"catalog": "analogs", "workbook": SimpleUploadedFile("dealer.xlsx", _book(rows))},
        follow=True,
    )
    batch = CatalogImportBatch.objects.order_by("-pk").first()
    client.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)
    batch.refresh_from_db()
    assert batch.status == CatalogImportBatch.Status.APPLIED, batch.error_text
    return AftermarketCatalogPart.objects.get(manufacturer_number="01.1395.100").part


def _stock_counters():
    return (
        StockLot.objects.count(),
        StockMovement.objects.count(),
        StockBalance.objects.count(),
        PartItem.objects.count(),
    )


def _receive(part, location, quantity="4", unit_cost="100"):
    """Настоящая приёмка: карточка наконец получает остаток и ячейку."""
    supplier, _ = Supplier.objects.get_or_create(name="ООО Поставка")
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, None)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(quantity))
    receive_stock_lot(lot, by=None)
    return lot


def _search(client, query="01.1395.100"):
    response = client.get(reverse("part_search"), {"q": query})
    assert response.status_code == 200
    return response.content.decode()


# --- Каталожная карточка без остатка -----------------------------------------


def test_the_imported_card_really_has_no_stock(boss):
    """Условие задачи: карточка есть, склада за ней нет."""
    part = _import(boss)
    assert part.recommended_price and part.recommended_price > 0
    assert _stock_counters() == (0, 0, 0, 0)


def test_search_calls_a_catalog_only_part_a_catalog_part(boss):
    part = _import(boss)
    body = _search(boss)

    assert "Каталог аналогов" in body
    assert "На складе" not in body
    assert "Доступно: <strong>0</strong>" in body
    # Ячейки нет: показывать было бы нечего и незачем.
    assert "<th>Ячейка</th>" not in body
    assert part.name.split()[0] in body


def test_looking_at_the_screen_creates_no_stock(boss):
    """Статус не подпирается ни лотом, ни движением, ни остатком."""
    part = _import(boss)
    before = _stock_counters()
    _search(boss)
    boss.get(reverse("part_detail", args=[part.pk]))
    assert _stock_counters() == before == (0, 0, 0, 0)


def test_the_part_card_names_the_source(boss):
    part = _import(boss)
    body = boss.get(reverse("part_detail", args=[part.pk])).content.decode()
    assert "Источник" in body
    assert "Каталог аналогов" in body
    assert "деталь ещё не принимали" in body


# --- После настоящей приёмки --------------------------------------------------


def test_after_real_stock_arrives_the_part_is_in_the_warehouse(boss):
    part = _import(boss)
    location = get_or_create_location("S01-D03-C08", name="Ящик 3 ячейка 8")
    _receive(part, location)

    body = _search(boss)
    assert "На складе" in body
    assert "Каталог аналогов" not in body
    assert "Доступно: <strong>4</strong>" in body
    assert "S01-D03-C08" in body

    card = boss.get(reverse("part_detail", args=[part.pk])).content.decode()
    assert "На складе" in card
    assert "Каталог аналогов" not in card


# --- Чужие карточки не должны попасть под подпись ------------------------------


def test_a_brp_part_without_stock_is_not_called_an_analog(boss):
    """У BRP свой источник; ноль остатка не делает деталь аналогом."""
    brp = BrpCatalogPart.objects.create(
        material_no="417224916", material_no_norm="417224916", part_desc="BRP BELT",
        wholesale_price_usd=Decimal("80"), retail_price_usd=Decimal("99.99"),
        is_current=True,
    )
    part = promote_to_warehouse(brp, by=None)
    body = _search(boss, "417224916")
    assert part.name.split()[0] in body
    assert "Каталог аналогов" not in body


def test_a_manual_part_without_stock_is_not_called_an_analog(boss):
    part = PartType.objects.create(
        name="ДЕТАЛЬ ВРУЧНУЮ", category=Category.objects.create(name="Разное"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("500"),
    )
    PartNumber.objects.create(
        part=part, value="RUCHNAYA-1", kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    body = _search(boss, "RUCHNAYA-1")
    assert "ДЕТАЛЬ ВРУЧНУЮ" in body
    assert "Каталог аналогов" not in body


# --- Пересчёт ячейки ------------------------------------------------------------


def test_counting_marks_the_scan_as_a_catalog_position(boss):
    part = _import(boss)
    location = get_or_create_location("S02-D01-C01", name="Ячейка")
    session = start_session(location=location, by=boss.user)

    line = record_scan(session, "01.1395.100", by=boss.user)
    assert line.warehouse_part_id == part.pk
    assert line.source == InventoryCountingLine.Source.AFTERMARKET
    assert line.get_source_display() == "Каталог аналогов"
    assert _stock_counters() == (0, 0, 0, 0)  # скан склад не трогает

    body = boss.get(reverse("counting_detail", args=[session.pk])).content.decode()
    assert "Каталог аналогов" in body


def test_posting_the_count_puts_the_part_in_the_warehouse(boss):
    part = _import(boss)
    location = get_or_create_location("S02-D01-C01", name="Ячейка")
    session = start_session(location=location, by=boss.user)
    record_scan(session, "01.1395.100", by=boss.user)
    record_scan(session, "01.1395.100", by=boss.user)

    post_session(session, by=boss.user)

    line = session.lines.get()
    assert line.quantity_counted == Decimal("2")
    assert line.source == InventoryCountingLine.Source.WAREHOUSE
    assert StockLot.objects.filter(part_type=part).exists()

    body = _search(boss)
    assert "На складе" in body
    assert "Каталог аналогов" not in body
    assert "Доступно: <strong>2</strong>" in body
    assert "S02-D01-C01" in body


def test_the_scan_message_speaks_russian(boss):
    """Оператор читает подпись, а не служебное слово из перечня."""
    _import(boss)
    location = get_or_create_location("S02-D01-C03", name="Ячейка 3")
    session = start_session(location=location, by=boss.user)

    response = boss.post(
        reverse("counting_scan", args=[session.pk]),
        {"code": "01.1395.100"},
        follow=True,
    )
    text = " ".join(str(m) for m in response.context["messages"])
    assert "Каталог аналогов" in text
    assert "aftermarket_catalog" not in text


def test_a_brp_scan_keeps_its_own_source(boss):
    BrpCatalogPart.objects.create(
        material_no="219800345", material_no_norm="219800345", part_desc="BELT DRIVE",
        wholesale_price_usd=Decimal("80"), retail_price_usd=Decimal("99.99"),
        is_current=True,
    )
    location = get_or_create_location("S02-D01-C02", name="Ячейка 2")
    session = start_session(location=location, by=boss.user)
    line = record_scan(session, "219800345", by=boss.user)
    assert line.source == InventoryCountingLine.Source.BRP
