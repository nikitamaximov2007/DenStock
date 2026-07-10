"""Layer 33.1 — ручное и приоритетное определение таможенной области применения.

Диагностика (предыдущая задача) подтвердила: импорт BRP/Polaris не создаёт
PartCompatibility, прайсы не содержат построчной применимости, поэтому
production-экспорт возвращал пустую колонку M для реальных деталей. Здесь -
контролируемый ручной редактор поверх уже существующего PartCustomsInfo.
application_area: приоритет 1) явное ручное значение, 2) автоопределение по
PartCompatibility, 3) пусто. Категория никогда не выводится из названия
детали, производителя каталога или формата артикула.

Фикстуры (env/_brp/_polaris/_sell/_login/_sheet/_compat) - копия из
test_customs_export.py: cross-file import ловит ruff F811 (параметр теста
"переопределяет" импортированное имя фикстуры), а per-file фикстуры - уже
принятый в проекте паттерн (test_actions.py/test_polaris.py дублируют так же).
"""
import importlib
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.actions.models import PartCustomsInfo
from apps.actions.services import part_export_data, perform_action
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse as promote_brp
from apps.catalog.models import PartCompatibility, VehicleMake, VehicleModel, VehicleType
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.polaris.models import PolarisCatalogPart
from apps.polaris.services import promote_to_warehouse as promote_polaris
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
SHEET = "Лист1"
DATA_ROW = 10

ApplicationArea = PartCustomsInfo.ApplicationArea


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
    return {"sup": sup, "loc": loc, "admin": admin}


def _brp(env, *, material, retail="10", wholesale="7", replacement="", desc="BELT DRIVE", qty=5):
    brp = BrpCatalogPart.objects.create(
        material_no=material, part_desc=desc,
        retail_price_usd=Decimal(retail), replacement_no_1=replacement,
        wholesale_price_usd=Decimal(wholesale) if wholesale is not None else None,
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


def _compat(part, make_name, vehicle_type_name, model_name="MODEL"):
    vtype, _ = VehicleType.objects.get_or_create(name=vehicle_type_name)
    make, _ = VehicleMake.objects.get_or_create(name=make_name, vehicle_type=vtype)
    model, _ = VehicleModel.objects.get_or_create(vehicle_make=make, name=model_name)
    PartCompatibility.objects.create(part=part, vehicle_model=model)


# --- 1-2. Явное значение попадает в Excel ----------------------------------------------


def test_explicit_snowmobile_reaches_excel(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    PartCustomsInfo.objects.create(part_type=part, application_area=ApplicationArea.SNOWMOBILE)
    _sell(env, part, number="219800345")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    assert sheet[f"M{DATA_ROW}"].value == "СНЕГОХОД"


def test_explicit_atv_reaches_excel(client, make_user, env):
    part, _ = _brp(env, material="420931285")
    PartCustomsInfo.objects.create(part_type=part, application_area=ApplicationArea.ATV)
    _sell(env, part, number="420931285")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    assert sheet[f"M{DATA_ROW}"].value == "КВАДРОЦИКЛ"


# --- 3-5. Порядок приоритетов -----------------------------------------------------------


def test_manual_value_wins_over_compatibility(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _compat(part, "Ski-Doo", "Снегоход", "SUMMIT")  # автоопределение дало бы СНЕГОХОД
    PartCustomsInfo.objects.create(
        part_type=part, application_area=ApplicationArea.WATERCRAFT
    )
    _sell(env, part, number="219800345")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    assert sheet[f"M{DATA_ROW}"].value == "ГИДРОЦИКЛ"  # явное значение победило


def test_compatibility_used_when_no_manual_value(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _compat(part, "Can-Am", "Квадроцикл", "OUTLANDER")
    row = part_export_data(part)
    assert row["application_area"] == "КВАДРОЦИКЛ"
    assert row["application_source"] == "compatibility"


def test_no_manual_and_no_compatibility_is_empty(client, make_user, env):
    part, _ = _brp(env, material="219800345")  # ни ручного значения, ни совместимости
    row = part_export_data(part)
    assert row["application_area"] == ""
    assert row["application_source"] == "none"


# --- 6. Легаси-значение не экспортируется ------------------------------------------------


def test_legacy_moto_zapchasti_not_exported(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    # Симулируем немигрированную строку старого прода: точный легаси-текст,
    # напрямую через ORM (choices не блокируют .create(), только формы/UI).
    PartCustomsInfo.objects.create(part_type=part, application_area="МОТО ЗАПЧАСТИ")
    _sell(env, part, number="219800345")
    _login(client, make_user)
    sheet = _sheet(client.get(reverse("actions_export")).content)
    assert sheet[f"M{DATA_ROW}"].value is None  # не «МОТО ЗАПЧАСТИ», а пусто


# --- 7-8. GET не создаёт PartCustomsInfo --------------------------------------------------


def test_report_page_get_does_not_create_customs_row(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _sell(env, part, number="219800345")
    before = PartCustomsInfo.objects.count()
    _login(client, make_user)
    resp = client.get(reverse("actions_report"))
    assert resp.status_code == 200
    assert PartCustomsInfo.objects.count() == before


def test_export_get_does_not_create_customs_row(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _sell(env, part, number="219800345")
    before = PartCustomsInfo.objects.count()
    _login(client, make_user)
    resp = client.get(reverse("actions_export"))
    assert resp.status_code == 200
    assert PartCustomsInfo.objects.count() == before


# --- 9-10. POST создаёт / обновляет запись ------------------------------------------------


def test_post_creates_customs_row(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    assert not PartCustomsInfo.objects.filter(part_type=part).exists()
    _login(client, make_user)
    resp = client.post(
        reverse("actions_customs_application", args=[part.pk]),
        {"application_area": ApplicationArea.SNOWMOBILE},
    )
    assert resp.status_code == 302
    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.application_area == "СНЕГОХОД"
    assert customs.updated_by is not None


def test_post_updates_existing_row(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    PartCustomsInfo.objects.create(part_type=part, application_area=ApplicationArea.SNOWMOBILE)
    _login(client, make_user)
    resp = client.post(
        reverse("actions_customs_application", args=[part.pk]),
        {"application_area": ApplicationArea.BOAT},
    )
    assert resp.status_code == 302
    assert PartCustomsInfo.objects.filter(part_type=part).count() == 1  # не задвоилось
    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.application_area == "КАТЕР / ЛОДКА"


# --- 11. Неавторизованный пользователь не может менять значение --------------------------


def test_unauthenticated_cannot_save(client, env):
    part, _ = _brp(env, material="219800345")
    resp = client.post(
        reverse("actions_customs_application", args=[part.pk]),
        {"application_area": ApplicationArea.SNOWMOBILE},
    )
    assert resp.status_code == 302  # login_required редиректит на вход
    assert "/login" in resp.url or "login" in resp.url
    assert not PartCustomsInfo.objects.filter(part_type=part).exists()


def test_viewer_role_cannot_save(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    make_user("viewer", role=roles.VIEWER)
    client.login(username="viewer", password=PASSWORD)
    resp = client.post(
        reverse("actions_customs_application", args=[part.pk]),
        {"application_area": ApplicationArea.SNOWMOBILE},
    )
    assert resp.status_code == 403
    assert not PartCustomsInfo.objects.filter(part_type=part).exists()


# --- 12. Нельзя сохранить категорию вне списка --------------------------------------------


def test_cannot_save_arbitrary_category(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _login(client, make_user)
    resp = client.post(
        reverse("actions_customs_application", args=[part.pk]),
        {"application_area": "ПОЕЗД"},
    )
    assert resp.status_code == 302
    assert not PartCustomsInfo.objects.filter(part_type=part).exists()  # ничего не создано


def test_cannot_overwrite_with_arbitrary_category(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    PartCustomsInfo.objects.create(part_type=part, application_area=ApplicationArea.SNOWMOBILE)
    _login(client, make_user)
    client.post(
        reverse("actions_customs_application", args=[part.pk]),
        {"application_area": "ПОЕЗД"},
    )
    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.application_area == "СНЕГОХОД"  # значение не испорчено


# --- 13-14. Точная идентичность ------------------------------------------------------------


def test_brp_and_polaris_same_number_not_mixed(client, make_user, env):
    brp_part, _ = _brp(env, material="3610075")
    pol_part, _ = _polaris(env, number="3610075")  # тот же номер, другой производитель
    _login(client, make_user)
    client.post(
        reverse("actions_customs_application", args=[brp_part.pk]),
        {"application_area": ApplicationArea.SNOWMOBILE},
    )
    client.post(
        reverse("actions_customs_application", args=[pol_part.pk]),
        {"application_area": ApplicationArea.WATERCRAFT},
    )
    assert (
        PartCustomsInfo.objects.get(part_type=brp_part).application_area == "СНЕГОХОД"
    )
    assert (
        PartCustomsInfo.objects.get(part_type=pol_part).application_area == "ГИДРОЦИКЛ"
    )


def test_replacement_compatibility_does_not_leak_to_exact_part(client, make_user, env):
    # Точная деталь без цены -> цена берётся от замены (существующее правило),
    # но замена продвинута в СВОЮ карточку склада с СВОЕЙ совместимостью.
    # Область применения точной детали не должна её унаследовать.
    exact_part, _ = _brp(
        env, material="250000059", retail="0", wholesale="0", replacement="250000418"
    )
    replacement_part, _ = _brp(env, material="250000418", retail="4.19", wholesale="3.29")
    _compat(replacement_part, "Ski-Doo", "Снегоход", "SUMMIT")
    row = part_export_data(exact_part)
    assert row["usd_price"] == Decimal("3.29")  # цена - от замены (уже покрыто)
    assert row["application_area"] == ""  # применимость замены не унаследована
    assert row["application_source"] == "none"


# --- 15-16. Счётчики и предупреждение на странице отчёта ---------------------------------


def test_report_page_readiness_counts(client, make_user, env):
    manual, _ = _brp(env, material="111000001")
    PartCustomsInfo.objects.create(part_type=manual, application_area=ApplicationArea.SNOWMOBILE)
    _sell(env, manual, number="111000001")

    via_compat, _ = _brp(env, material="222000002")
    _compat(via_compat, "Can-Am", "Квадроцикл", "OUTLANDER")
    _sell(env, via_compat, number="222000002")

    missing, _ = _brp(env, material="333000003")
    _sell(env, missing, number="333000003")

    _login(client, make_user)
    html = client.get(reverse("actions_report")).content.decode()
    assert "Готово: 2" in html
    assert "не заполнено: 1" in html


def test_missing_application_warning_shown_and_hidden(client, make_user, env):
    part, _ = _brp(env, material="219800345")
    _sell(env, part, number="219800345")
    _login(client, make_user)
    html = client.get(reverse("actions_report")).content.decode()
    assert "Позиций без области применения: 1" in html

    PartCustomsInfo.objects.create(part_type=part, application_area=ApplicationArea.SNOWMOBILE)
    html = client.get(reverse("actions_report")).content.decode()
    assert "Позиций без области применения" not in html


# --- 17-18. Data migration -----------------------------------------------------------------


def _run_data_migration():
    from django.apps import apps as live_apps

    module = importlib.import_module(
        "apps.actions.migrations.0006_clear_legacy_application_area"
    )
    module.clear_legacy(live_apps, None)


def test_data_migration_clears_exact_legacy_value(db, env):
    part, _ = _brp(env, material="219800345")
    PartCustomsInfo.objects.create(part_type=part, application_area="МОТО ЗАПЧАСТИ")
    _run_data_migration()
    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.application_area == ""


def test_data_migration_does_not_touch_other_values(db, env):
    part1, _ = _brp(env, material="111000001")
    part2, _ = _brp(env, material="222000002")
    part3, _ = _brp(env, material="333000003")
    PartCustomsInfo.objects.create(part_type=part1, application_area=ApplicationArea.SNOWMOBILE)
    PartCustomsInfo.objects.create(part_type=part2, application_area="")
    # Неизвестное значение (не легаси, не из списка) - миграция не должна его удалить.
    PartCustomsInfo.objects.create(part_type=part3, application_area="НЕИЗВЕСТНАЯ КАТЕГОРИЯ")
    _run_data_migration()
    assert PartCustomsInfo.objects.get(part_type=part1).application_area == "СНЕГОХОД"
    assert PartCustomsInfo.objects.get(part_type=part2).application_area == ""
    assert (
        PartCustomsInfo.objects.get(part_type=part3).application_area
        == "НЕИЗВЕСТНАЯ КАТЕГОРИЯ"
    )


# --- 19. Полный XLSX остаётся валидным ------------------------------------------------------


def test_full_workbook_stays_valid_with_mixed_sources(client, make_user, env):
    manual, _ = _brp(env, material="111000001")
    PartCustomsInfo.objects.create(part_type=manual, application_area=ApplicationArea.CAR)
    _sell(env, manual, number="111000001")

    via_compat, _ = _brp(env, material="222000002")
    _compat(via_compat, "Sea-Doo", "Гидроцикл", "GTX")
    _sell(env, via_compat, number="222000002")

    empty, _ = _brp(env, material="333000003")
    _sell(env, empty, number="333000003")

    pol, _ = _polaris(env, number="3610075")
    _sell(env, pol, number="3610075")

    _login(client, make_user)
    resp = client.get(reverse("actions_export"))
    assert resp.status_code == 200
    workbook = openpyxl.load_workbook(BytesIO(resp.content))  # без предупреждений о повреждении
    sheet = workbook[SHEET]
    values = [sheet[f"M{DATA_ROW + i}"].value for i in range(4)]
    assert "АВТОМОБИЛЬ" in values
    assert "ГИДРОЦИКЛ" in values
    assert values.count(None) == 2  # пустая совместимость + Polaris без применимости
