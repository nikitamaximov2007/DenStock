"""Таможенный Excel-экспорт: регрессия production-500 и контракт файла.

Первопричина 500 на production: шаблон «Формы для заказа» лежал в docs/,
а docs/ исключён из Docker-образа (.dockerignore) — openpyxl.load_workbook
падал с FileNotFoundError. Шаблон перенесён в пакет приложения.

Здесь же закреплён фактический контракт файла (лист, колонки, группировка,
exact identity, фильтры) и защита Excel: управляющие символы и formula
injection. Экспорт read-only: ни движений, ни действий, ни лотов не меняет.
"""
import datetime
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.models import PartCustomsInfo, WarehouseAction
from apps.actions.services import (
    TEMPLATE_PATH,
    ActionError,
    cancel_warehouse_action,
    excel_safe_text,
    perform_action,
)
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse as promote_brp
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.polaris.models import PolarisCatalogPart
from apps.polaris.services import promote_to_warehouse as promote_polaris
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET = "Лист1"
DATA_ROW = 10


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


@pytest.fixture
def env(db, admin):
    sup = Supplier.objects.create(name="Стартовый ввод")
    loc = StorageLocation.objects.create(
        name="C04", code="S04-L03-D01-C04", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="C03", code="S04-L03-D01-C03", storage_allowed=True, is_active=True
    )
    return {"sup": sup, "loc": loc, "loc2": loc2, "admin": admin}


def _brp(env, *, material, retail="10", replacement="", desc="BELT DRIVE", qty=5):
    brp = BrpCatalogPart.objects.create(
        material_no=material, part_desc=desc,
        retail_price_usd=Decimal(retail), replacement_no_1=replacement,
    )
    part = promote_brp(brp, by=env["admin"])
    _stock(part, env["loc"], qty, env["sup"], env["admin"])
    return part, brp


def _polaris(env, *, number, wholesale="6", retail="20", superseded="", desc="SEAL", qty=5):
    pol = PolarisCatalogPart.objects.create(
        part_number=number, part_name=desc, superseded_number=superseded,
        wholesale_price_usd=Decimal(wholesale) if wholesale is not None else None,
        retail_price_usd=Decimal(retail) if retail is not None else None,
    )
    part = promote_polaris(pol, by=env["admin"])
    _stock(part, env["loc"], qty, env["sup"], env["admin"])
    return part, pol


def _warehouse_only(env, *, number="WH-500", name="РУЧНАЯ ДЕТАЛЬ", qty=5):
    part = PartType.objects.create(
        name=name, category=Category.objects.create(name=f"cat-{number}"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM, is_primary=True)
    _stock(part, env["loc"], qty, env["sup"], env["admin"])
    return part


def _sell(env, part, *, qty="1", number="", comment="Иванов", location=None):
    return perform_action(
        part=part, location=location or env["loc"], action_type="sale",
        quantity=qty, customer_comment=comment, scanned_number=number, by=env["admin"],
    )


def _login(client, make_user, *, superuser=True, name="boss"):
    make_user(name, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


def _sheet(content: bytes):
    return openpyxl.load_workbook(BytesIO(content))[SHEET]


def _b_column(sheet, count=6):
    return [sheet[f"B{DATA_ROW + i}"].value for i in range(count)]


# --- Регрессия production-500: шаблон должен ехать в образ -----------------------------


def test_template_lives_inside_app_package_not_docs():
    """Первопричина 500: шаблон был в docs/, а docs/ исключён из Docker-образа."""
    assert TEMPLATE_PATH.exists(), TEMPLATE_PATH
    parts = TEMPLATE_PATH.parts
    assert "docs" not in parts, "рантайм-ассет не должен лежать в docs/"
    assert "actions" in parts and "customs_template" in parts


def test_template_path_not_excluded_by_dockerignore():
    """Regression: любой путь шаблона, попавший под .dockerignore, ломает export."""
    from django.conf import settings

    root = TEMPLATE_PATH.parents[3]  # .../apps/actions/customs_template/file -> repo root
    ignore = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    excluded = {
        line.strip().rstrip("/")
        for line in ignore
        if line.strip() and not line.startswith(("#", "!"))
    }
    relative = TEMPLATE_PATH.relative_to(settings.BASE_DIR).parts
    assert not set(relative) & excluded, (
        f"шаблон {relative} исключён из образа правилами {excluded & set(relative)}"
    )


def test_missing_template_raises_clear_error(env, monkeypatch):
    """Без шаблона — понятная ActionError, а не голый FileNotFoundError."""
    import apps.actions.services as svc

    monkeypatch.setattr(svc, "TEMPLATE_PATH", TEMPLATE_PATH.with_name("нет-файла.xlsx"))
    with pytest.raises(ActionError, match="Шаблон таможенной формы не найден"):
        svc.export_customs_xlsx([])


# --- HTTP-контракт ---------------------------------------------------------------------


def test_export_returns_valid_xlsx(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _sell(env, part, number="219800345")
    _login(client, make_user)
    resp = client.get(reverse("actions_export"))
    assert resp.status_code == 200
    assert resp["Content-Type"] == XLSX_MIME
    assert ".xlsx" in resp["Content-Disposition"]
    assert resp["Content-Disposition"].isascii()  # безопасное имя файла
    workbook = openpyxl.load_workbook(BytesIO(resp.content))
    assert SHEET in workbook.sheetnames


def test_headers_preserved_in_order(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _sell(env, part, number="219800345")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    headers = [sheet[f"{c}6"].value for c in "ABCDEFGHIJKLM"]
    assert headers[1] and "АРТИКУЛ" in str(headers[1]).upper()
    assert headers[9] and "КОЛИЧЕСТВО" in str(headers[9]).upper()
    assert sheet["I10"].value == "=J10*G10"  # формулы шаблона сохранены
    assert sheet["L10"].value == "=K10*J10"


def test_empty_selection_does_not_500(client, make_user, env):
    _login(client, make_user)
    resp = client.get(reverse("actions_export") + "?part_number=НЕТ-ТАКОГО")
    assert resp.status_code == 200
    sheet = _sheet(resp.content)
    assert sheet[f"B{DATA_ROW}"].value is None  # пример шаблона очищен, данных нет


# --- Exact identity --------------------------------------------------------------------


def test_brp_exports_exact_material_no(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _sell(env, part, number="219800345")
    _login(client, make_user)
    assert "219800345" in _b_column(_sheet(client.get(reverse("actions_export")).content))


def test_brp_replacement_does_not_replace_number(client, make_user, env):
    # У exact 420931285 цена 0; цена берётся из replacement 420931284.
    BrpCatalogPart.objects.create(
        material_no="420931284", part_desc="OLD", retail_price_usd=Decimal("4"),
        replacement_no_1="420931285",
    )
    part, _ = _brp(env, material="420931285", retail="0", replacement="420931284")
    _sell(env, part, number="420931285")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    numbers = _b_column(sheet)
    assert "420931285" in numbers  # exact identity
    assert "420931284" not in numbers  # replacement — только источник цены
    assert sheet[f"K{DATA_ROW}"].value == Decimal("4")  # цена от источника


def test_polaris_exports_exact_part_number(client, make_user, env):
    part, _ = _polaris(env, number="3610075")
    _sell(env, part, number="3610075")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    assert "3610075" in _b_column(sheet)
    assert sheet[f"E{DATA_ROW}"].value == "POLARIS"


def test_polaris_superseded_does_not_replace_number(client, make_user, env):
    PolarisCatalogPart.objects.create(
        part_number="1111111", part_name="OLD", retail_price_usd=Decimal("15"),
        wholesale_price_usd=Decimal("8"), superseded_number="2222222",
    )
    part, _ = _polaris(env, number="2222222", retail="0", wholesale="0", superseded="1111111")
    _sell(env, part, number="2222222")
    _login(client, make_user)
    numbers = _b_column(_sheet(client.get(reverse("actions_export")).content))
    assert "2222222" in numbers
    assert "1111111" not in numbers


def test_warehouse_only_uses_snapshot_number(client, make_user, env):
    part = _warehouse_only(env, number="WH-500")
    action = _sell(env, part, number="WH-500")
    assert action.part_number == "WH-500"
    _login(client, make_user)
    assert "WH-500" in _b_column(_sheet(client.get(reverse("actions_export")).content))


# --- Группировка -----------------------------------------------------------------------


def test_same_exact_number_groups_and_sums_quantity(client, make_user, env):
    part, _ = _brp(env, material="219800345", qty=10)
    _sell(env, part, qty="2", number="219800345")
    _sell(env, part, qty="3", number="219800345")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    assert _b_column(sheet, 3)[:2] == ["219800345", None]  # одна строка
    assert Decimal(str(sheet[f"J{DATA_ROW}"].value)) == Decimal("5")


def test_different_exact_numbers_of_same_part_not_merged(client, make_user, env):
    part = _warehouse_only(env, number="WH-100", qty=10)
    PartNumber.objects.create(part=part, value="WH-200", kind=PartNumber.Kind.ARTICLE)
    _sell(env, part, number="WH-100")
    _sell(env, part, number="WH-200")
    _login(client, make_user)
    numbers = _b_column(_sheet(client.get(reverse("actions_export")).content))
    assert "WH-100" in numbers and "WH-200" in numbers  # не слиты в одну строку


def test_same_number_brp_and_polaris_not_merged(client, make_user, env):
    brp_part, _ = _brp(env, material="5555555")
    pol_part, _ = _polaris(env, number="5555555")
    _sell(env, brp_part, number="5555555")
    _sell(env, pol_part, number="5555555")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    rows = [
        (sheet[f"B{DATA_ROW + i}"].value, sheet[f"E{DATA_ROW + i}"].value) for i in range(2)
    ]
    assert sorted(rows) == [("5555555", "BRP"), ("5555555", "POLARIS")]


# --- Фильтры отчёта --------------------------------------------------------------------


def test_cancelled_excluded_by_default(client, make_user, env):
    part, _ = _brp(env, material="219800345", qty=10)
    bad = _sell(env, part, qty="2", number="219800345")
    _sell(env, part, qty="3", number="219800345")
    cancel_warehouse_action(bad, by=env["admin"], reason="Дубль")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    assert Decimal(str(sheet[f"J{DATA_ROW}"].value)) == Decimal("3")  # без отменённых


def test_cancelled_flag_does_not_leak_into_customs(client, make_user, env):
    """Контракт: даже с cancelled=1 в отчёте таможня считает только активные."""
    part, _ = _brp(env, material="219800345", qty=10)
    bad = _sell(env, part, qty="2", number="219800345")
    _sell(env, part, qty="3", number="219800345")
    cancel_warehouse_action(bad, by=env["admin"], reason="Дубль")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export") + "?cancelled=1").content)
    assert Decimal(str(sheet[f"J{DATA_ROW}"].value)) == Decimal("3")


def test_date_filter_applied(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _sell(env, part, number="219800345")
    _login(client, make_user)
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    sheet = _sheet(client.get(reverse("actions_export") + f"?date_from={tomorrow}").content)
    assert sheet[f"B{DATA_ROW}"].value is None  # всё отфильтровано


def test_type_filter_applied(client, make_user, env):
    part, _ = _brp(env, material="219800345", qty=10)
    _sell(env, part, number="219800345")
    perform_action(part=part, location=env["loc"], action_type="reserve", quantity="1",
                   customer_comment="Петров", scanned_number="219800345", by=env["admin"])
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export") + "?action_type=reserve").content)
    assert Decimal(str(sheet[f"J{DATA_ROW}"].value)) == Decimal("1")  # только резерв


def test_part_number_filter_applied(client, make_user, env):
    a, _ = _brp(env, material="219800345")
    b, _ = _brp(env, material="700700700")
    _sell(env, a, number="219800345")
    _sell(env, b, number="700700700")
    _login(client, make_user)
    resp = client.get(reverse("actions_export") + "?part_number=700700700")
    numbers = _b_column(_sheet(resp.content))
    assert "700700700" in numbers and "219800345" not in numbers


def test_location_filter_applied(client, make_user, env):
    part, _ = _brp(env, material="219800345", qty=10)
    _stock(part, env["loc2"], 5, env["sup"], env["admin"])
    _sell(env, part, number="219800345", location=env["loc2"])
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export") + "?location_code=S04-L03-D01-C03").content)
    assert "219800345" in _b_column(sheet)
    sheet = _sheet(client.get(reverse("actions_export") + "?location_code=НЕТ").content)
    assert sheet[f"B{DATA_ROW}"].value is None


# --- Неполные данные и Decimal ----------------------------------------------------------


def test_missing_price_weights_and_customs_do_not_500(client, make_user, env):
    part, _ = _brp(env, material="777000111", retail="0")  # нет цены USD, нет весов
    _sell(env, part, number="777000111")
    assert not PartCustomsInfo.objects.filter(part_type=part).exists()
    _login(client, make_user)
    resp = client.get(reverse("actions_export"))
    assert resp.status_code == 200
    sheet = _sheet(resp.content)
    assert sheet[f"K{DATA_ROW}"].value is None  # цена пустая, не выдумана
    assert sheet[f"G{DATA_ROW}"].value is None and sheet[f"H{DATA_ROW}"].value is None


def test_decimal_quantity_written_as_number(client, make_user, env):
    part, _ = _brp(env, material="219800345", qty=10)
    _sell(env, part, qty="2", number="219800345")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    assert Decimal(str(sheet[f"J{DATA_ROW}"].value)) == Decimal("2")
    assert Decimal(str(sheet[f"K{DATA_ROW}"].value)) == Decimal("10")


# --- Безопасность Excel ------------------------------------------------------------------


def test_excel_safe_text_strips_control_chars_and_neutralises_formulas():
    assert excel_safe_text("BAD\x0bTEXT") == "BADTEXT"
    assert excel_safe_text("=SUM(A1)") == "'=SUM(A1)"
    assert excel_safe_text("+1") == "'+1"
    assert excel_safe_text("-1") == "'-1"
    assert excel_safe_text("@cmd") == "'@cmd"
    assert excel_safe_text("РОЛИК-ШКИВ 420931285") == "РОЛИК-ШКИВ 420931285"  # не искажён
    assert excel_safe_text(None) is None
    assert excel_safe_text("") is None
    assert len(excel_safe_text("я" * 40000)) == 32767


def test_control_character_in_name_does_not_break_workbook(client, make_user, env):
    part, _ = _brp(env, material="219800345", desc="BELT\x0bDRIVE")
    _sell(env, part, number="219800345")
    _login(client, make_user)
    resp = client.get(reverse("actions_export"))
    assert resp.status_code == 200  # раньше — IllegalCharacterError -> 500
    sheet = _sheet(resp.content)
    assert "\x0b" not in str(sheet[f"D{DATA_ROW}"].value)


def test_formula_like_name_is_not_a_formula(client, make_user, env):
    part, _ = _brp(env, material="219800345", desc="=HYPERLINK(1)")
    _sell(env, part, number="219800345")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    value = str(sheet[f"D{DATA_ROW}"].value)
    assert value.startswith("'=")  # текст, не формула
    assert sheet[f"D{DATA_ROW}"].data_type != "f"


# --- Read-only -----------------------------------------------------------------------------


def test_export_does_not_mutate_database(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _sell(env, part, number="219800345")
    before = (
        StockMovement.objects.count(),
        WarehouseAction.objects.count(),
        StockLot.objects.count(),
        PartCustomsInfo.objects.count(),
    )
    _login(client, make_user)
    assert client.get(reverse("actions_export")).status_code == 200
    after = (
        StockMovement.objects.count(),
        WarehouseAction.objects.count(),
        StockLot.objects.count(),
        PartCustomsInfo.objects.count(),
    )
    assert before == after  # GET-экспорт ничего не пишет


def test_historical_snapshot_survives_rate_change(client, make_user, env):
    """Смена курса не переписывает исторические суммы действия."""
    from apps.warehouse.models import ValuationSettings

    part, _ = _brp(env, material="219800345")
    action = _sell(env, part, number="219800345")
    snapshot = (action.part_number, action.unit_price_rub, action.total_price_rub)
    settings_row = ValuationSettings.get()
    settings_row.current_usd_rate = Decimal("200")
    settings_row.save()
    action.refresh_from_db()
    assert (action.part_number, action.unit_price_rub, action.total_price_rub) == snapshot
    _login(client, make_user)
    assert client.get(reverse("actions_export")).status_code == 200
