"""Операторский экран каталога BRP: лишняя колонка убрана, цена не тронута.

Розничная цена BRP в долларах стояла рядом с ценой клиента в рублях и путала
оператора: продают по рублёвой, а глаз цеплялся за долларовую. Колонка убрана
с экрана, но не из данных - розница остаётся исходным полем каталога и
снимком на связи со складской карточкой.

Цена клиента считается от ОПТОВОЙ цены и от розницы не зависела никогда.
Здесь это закреплено прямо: изменение розницы цену клиента не двигает.
"""
from decimal import Decimal

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.brp.importer import import_catalog
from apps.brp.models import BrpCatalogPart, BrpPartLink, BrpPricingSettings
from apps.brp.pricing import catalog_part_price_rub, customer_price_rub
from apps.brp.services import promote_to_warehouse
from apps.warehouse.models import ValuationSettings

PASSWORD = "parol-12345"
HEADERS = [
    "Material_No", "Part_Desc", "Last_Yr_Util", "Status",
    "РОЗНИЦА", "ОПТОВАЯ", "ЗАМЕНА НОМЕРА", "ЗАМЕНА НОМЕРА",
]
ROWS = [
    ["417224916", "ROLLER PULLEY", 2025, None, 35.99, 28.15, None, None],
    ["353589", "SCREW M6X16", 2025, "LIQ", 9.03, 7.03, None, None],
]


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
def catalog(db, tmp_path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    sheet.append([None] * 8)
    for row in ROWS:
        sheet.append(row)
    path = tmp_path / "brp.xlsx"
    workbook.save(path)
    import_catalog(path, commit=True)
    return BrpCatalogPart.objects.get(material_no="417224916")


def _login(client, make_user, *, role=None, name="boss"):
    make_user(name, role=role, is_superuser=role is None)
    client.login(username=name, password=PASSWORD)


def _search(client, query="417224916"):
    return client.get(reverse("brp_search") + f"?q={query}").content.decode()


# --- Экран ------------------------------------------------------------------


def test_retail_usd_column_is_gone(client, catalog, make_user):
    _login(client, make_user)
    html = _search(client)
    assert "ROLLER PULLEY" in html  # позиция найдена
    assert "Розница BRP" not in html
    assert "Розница BRP ($)" not in html


def test_customer_rub_price_stays_on_screen(client, catalog, make_user):
    _login(client, make_user)
    html = _search(client)
    assert "Цена (₽)" in html  # единый операторский словарь


def test_customer_rub_value_is_the_right_number(client, catalog, make_user):
    """На экране именно рассчитанная цена клиента, а не доллары каталога."""
    rate = ValuationSettings.get()
    rate.current_usd_rate = Decimal("100")
    rate.save()
    markup = BrpPricingSettings.get()
    markup.brp_markup_percent = Decimal("40")
    markup.save()

    _login(client, make_user)
    html = _search(client)
    expected = customer_price_rub(Decimal("28.15"), Decimal("100"), Decimal("40"))
    assert expected == Decimal("3941")  # 28.15 * 100 * 1.4
    assert "3 941" in html.replace(" ", " ")
    assert "35.99" not in html  # розница каталога на экран не выводится
    assert "28.15" not in html  # оптовая тоже: оператор видит рубли


def test_retail_usd_absent_across_the_whole_result_table(client, catalog, make_user):
    """Проверка не по одной строке: розницы нет ни у одной позиции таблицы."""
    _login(client, make_user)
    html = _search(client, query="SCREW")
    assert "SCREW M6X16" in html
    for retail in ("9.03", "35.99"):
        assert retail not in html


def test_table_head_and_body_still_line_up(client, catalog, make_user):
    """Убирая колонку, легко забыть ячейку и сдвинуть всю таблицу."""
    import re

    _login(client, make_user)
    html = _search(client)
    for table in re.findall(r"<table.*?</table>", html, re.S):
        head = re.search(r"<thead>.*?</thead>", table, re.S)
        body = re.search(r"<tbody>.*?<tr>(.*?)</tr>", table, re.S)
        if not head or not body:
            continue
        assert len(re.findall(r"<th[ >]", head.group(0))) == len(
            re.findall(r"<td[ >]", body.group(1))
        )


# --- Данные и формула -------------------------------------------------------


def test_source_retail_usd_is_still_stored(catalog):
    """Колонку убрали с экрана, но не из каталога."""
    assert catalog.retail_price_usd == Decimal("35.99")
    assert BrpCatalogPart.objects.get(material_no="353589").retail_price_usd == Decimal("9.03")


def test_promotion_still_snapshots_retail_usd(catalog, make_user):
    """Снимок розницы на связи со складом остаётся: это аудит продвижения."""
    admin = make_user("admin", is_superuser=True)
    part = promote_to_warehouse(catalog, by=admin)
    link = BrpPartLink.objects.get(part=part)
    assert link.brp_retail_price_usd == Decimal("35.99")
    assert link.brp_wholesale_price_usd == Decimal("28.15")


def test_customer_price_is_driven_by_wholesale_not_retail(catalog):
    before = catalog_part_price_rub(catalog, Decimal("100"), Decimal("40"))
    catalog.retail_price_usd = Decimal("999.99")
    catalog.save(update_fields=["retail_price_usd"])
    catalog.refresh_from_db()
    after = catalog_part_price_rub(catalog, Decimal("100"), Decimal("40"))
    assert after == before  # розница цену клиента не двигает
    assert before == Decimal("3941")


def test_wholesale_still_drives_the_price(catalog):
    before = catalog_part_price_rub(catalog, Decimal("100"), Decimal("40"))
    catalog.wholesale_price_usd = Decimal("30")
    catalog.save(update_fields=["wholesale_price_usd"])
    catalog.refresh_from_db()
    after = catalog_part_price_rub(catalog, Decimal("100"), Decimal("40"))
    assert after == Decimal("4200")  # 30 * 100 * 1.4
    assert after != before


def test_missing_wholesale_still_yields_no_price(catalog):
    catalog.wholesale_price_usd = None
    catalog.save(update_fields=["wholesale_price_usd"])
    catalog.refresh_from_db()
    # Розница на месте, но подставлять её вместо оптовой нельзя.
    assert catalog.retail_price_usd == Decimal("35.99")
    assert catalog_part_price_rub(catalog, Decimal("100"), Decimal("40")) is None


# --- Права ------------------------------------------------------------------


def test_catalog_screen_permissions(client, catalog, make_user):
    url = reverse("brp_search") + "?q=417224916"
    assert "login" in client.get(url).url  # без входа

    make_user("watcher", role=roles.VIEWER)
    client.login(username="watcher", password=PASSWORD)
    html = client.get(url).content.decode()
    assert client.get(url).status_code == 200
    assert "Розница BRP" not in html  # колонки нет и у наблюдателя
