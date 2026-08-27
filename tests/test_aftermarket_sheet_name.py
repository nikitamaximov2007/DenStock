"""Лист прайса находится и тогда, когда поставщик приписал к нему своё имя.

Настоящие книги поставщиков приходят с вкладкой вида «diorlight priceupdate»,
а не с голым «priceupdate». Ключевое слово при этом на месте, и отказывать
такой книге не за что: импортёр искал точное совпадение и не видел лист,
который человек видит глазами.

Угадывать при этом нельзя. Если листов с прайсом несколько, книга
отклоняется с перечислением: какой из них верный, решает человек.
"""
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from apps.catalog_import.aftermarket_catalog import (
    AftermarketCatalogError,
    _price_sheet_name,
)
from apps.catalog_import.models import AftermarketCatalogPart, CatalogImportBatch
from apps.inventory.models import StockBalance, StockLot, StockMovement

HEADERS = ["Manufacturer", "Item SKU", "Manufacturer Number", "Description", "Dlr Cost"]
ROW = ["WOODYS", "AA6-9750", "AA6-9750", "ACE 6 X 60 TURNING", "107.6"]
PASSWORD = "parol-12345"


@pytest.fixture
def boss(db, django_user_model, client):
    django_user_model.objects.create_superuser(username="boss", password=PASSWORD)
    client.login(username="boss", password=PASSWORD)
    return client


def _book(sheet_names, *, data_on=0):
    """Книга с указанными листами; данные кладутся на лист с номером data_on."""
    book = Workbook()
    book.remove(book.active)
    for name in sheet_names:
        book.create_sheet(name)
    sheet = book[sheet_names[data_on]]
    sheet.append(HEADERS)
    sheet.append([""] * len(HEADERS))
    sheet.append(ROW)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def _upload(client, payload):
    return client.post(
        reverse("catalog_import_upload"),
        {"catalog": "analogs", "workbook": SimpleUploadedFile("dealer.xlsx", payload)},
        follow=True,
    )


# --- Выбор листа ------------------------------------------------------------


@pytest.mark.parametrize(
    "names, expected",
    [
        (["priceupdate"], "priceupdate"),
        (["diorlight priceupdate"], "diorlight priceupdate"),
        (["PriceUpdate 2026"], "PriceUpdate 2026"),
        (["Лист1", "diorlight priceupdate"], "diorlight priceupdate"),
        (["price update"], "price update"),  # пробел внутри слова не мешает
    ],
)
def test_canonical_sheet_is_found(names, expected):
    assert _price_sheet_name(names) == expected


@pytest.mark.parametrize("names", [[], ["Лист1"], ["Sheet1", "Прайс"]])
def test_missing_sheet_is_named_in_the_refusal(names):
    with pytest.raises(AftermarketCatalogError, match="нужен лист priceupdate"):
        _price_sheet_name(names)


def test_several_price_sheets_are_refused_not_guessed():
    with pytest.raises(AftermarketCatalogError, match="несколько листов"):
        _price_sheet_name(["a priceupdate", "b priceupdate"])


def test_the_refusal_lists_what_was_actually_found():
    with pytest.raises(AftermarketCatalogError) as error:
        _price_sheet_name(["Лист1", "Прайс 2026"])
    assert "Лист1" in str(error.value) and "Прайс 2026" in str(error.value)


# --- Сквозь настоящий экран импорта -----------------------------------------


def test_supplier_prefixed_sheet_imports_through_the_screen(boss):
    _upload(boss, _book(["diorlight priceupdate"]))
    batch = CatalogImportBatch.objects.get()
    assert batch.catalog == "aftermarket"

    before = (StockBalance.objects.count(), StockLot.objects.count(),
              StockMovement.objects.count())
    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)
    batch.refresh_from_db()
    assert batch.status == CatalogImportBatch.Status.APPLIED

    entry = AftermarketCatalogPart.objects.get()
    assert entry.manufacturer.name == "WOODYS"
    assert entry.manufacturer_number == "AA6-9750"
    assert entry.dealer_cost_usd == Decimal("107.60")
    assert entry.msrp_usd is None  # колонки MSRP в книге нет - её не выдумываем
    # Каталог не склад: остатки не появляются от того, что заведена карточка.
    assert (StockBalance.objects.count(), StockLot.objects.count(),
            StockMovement.objects.count()) == before


def test_workbook_without_a_price_sheet_is_not_imported(boss):
    """Книга без листа прайса не становится каталогом аналогов молча.

    Формат не опознаётся как aftermarket, разбирать её берётся выбранный
    пользователем адаптер и сообщает о своей ошибке. Важно здесь одно:
    ничего не применилось.
    """
    _upload(boss, _book(["Лист1"]))
    batch = CatalogImportBatch.objects.order_by("-pk").first()
    assert batch is None or batch.status != CatalogImportBatch.Status.APPLIED
    assert AftermarketCatalogPart.objects.count() == 0


def test_two_price_sheets_are_refused_on_screen(boss):
    response = _upload(boss, _book(["one priceupdate", "two priceupdate"]))
    assert "несколько листов" in response.content.decode()
    assert AftermarketCatalogPart.objects.count() == 0
