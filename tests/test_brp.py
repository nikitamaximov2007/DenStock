"""Layer 31 — BRP-каталог: импорт прайса, цены, продвижение в склад, адреса.

Ключевые гарантии: импорт 127k-строчного прайса — ТОЛЬКО справочник (ни
остатков, ни движений, ни поступлений), идемпотентен, Material_No хранится
строкой; цена клиента считается по формуле без округления на Decimal;
продвижение фиксирует курс/наценку навсегда; наличие появляется только после
проведения документа; адреса хранения собираются в формате B-S01-L02-D03-C08.
В тестах используется маленький сгенерированный xlsx, не реальный прайс.
"""
from decimal import Decimal

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse

from apps.accounts import roles
from apps.brp.importer import import_catalog
from apps.brp.models import BrpCatalogPart, BrpPartLink, BrpPricingSettings
from apps.brp.pricing import customer_price_rub
from apps.brp.services import get_or_create_intake_draft, promote_to_warehouse
from apps.catalog.models import PartNumber, PartType
from apps.inventory.models import StockBalance, StockMovement
from apps.procurement.models import Batch
from apps.receipts.models import Receipt
from apps.receipts.services import add_line, post_receipt
from apps.warehouse.addresses import AddressError, compose_address
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"

HEADERS = [
    "Material_No", "Part_Desc", "Last_Yr_Util", "Status",
    "РОЗНИЦА", "ОПТОВАЯ", "ЗАМЕНА НОМЕРА", "ЗАМЕНА НОМЕРА",
]


def _make_xlsx(tmp_path, rows, name="brp-sample.xlsx"):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    sheet.append([None] * 8)  # строка 2: примечания (пустая в колонках данных)
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    return path


SAMPLE_ROWS = [
    # material, desc, year, status, розница, оптовая, замена1, замена2
    ["042", "BUSHING              ", 2025, None, 6.49, 5.29, "204130036", 460041],
    [324816, "SCREW", 2025, None, 2.01, 1.77, "769244", "423324816"],
    [335220, "SCREW OLD", 2005, "USE", 0, 0, None, 463185],
    [353589, "SCREW M6X16", 2025, "LIQ", 9.03, None, None, None],
    [324816, "SCREW DUPLICATE", 2025, None, 9.99, 9.99, None, None],  # дубль
    [None, None, None, None, None, None, None, None],  # пустая строка
]


def _stock_snapshot():
    return {
        "balances": StockBalance.objects.count(),
        "movements": StockMovement.objects.count(),
        "batches": Batch.objects.count(),
        "receipts": Receipt.objects.count(),
        "parts": PartType.objects.count(),
    }


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
def sample_file(tmp_path):
    return _make_xlsx(tmp_path, SAMPLE_ROWS)


@pytest.fixture
def imported(db, sample_file):
    import_catalog(sample_file, commit=True)
    return sample_file


# --- Импорт ---------------------------------------------------------------------


def test_dry_run_creates_nothing(db, sample_file):
    summary = import_catalog(sample_file, commit=False)
    assert summary.mode == "dry-run"
    assert summary.created == 4
    assert BrpCatalogPart.objects.count() == 0  # dry-run ничего не пишет


def test_commit_parses_all_fields(db, sample_file):
    summary = import_catalog(sample_file, commit=True)
    assert summary.data_rows == 4
    assert summary.created == 4
    assert summary.duplicates == 1
    assert summary.skipped_empty == 2  # строка 2 (примечания) + пустая строка
    assert summary.unique_materials == 4
    assert summary.with_retail_price == 4
    assert summary.with_wholesale_price == 3
    assert summary.with_replacement == 3
    assert summary.status_counts == {"USE": 1, "LIQ": 1}

    part = BrpCatalogPart.objects.get(material_no="042")
    assert part.material_no == "042"  # строка: ведущий ноль не потерян
    assert part.part_desc == "BUSHING"  # хвостовые пробелы обрезаны
    assert part.retail_price_usd == Decimal("6.49")
    assert part.wholesale_price_usd == Decimal("5.29")
    assert part.replacement_no_1 == "204130036"
    assert part.replacement_no_2 == "460041"  # int из Excel -> строка
    assert part.last_year_util == "2025"
    assert part.source_row == 3

    old = BrpCatalogPart.objects.get(material_no="335220")
    assert old.brp_status == "USE"
    assert old.retail_price_usd == Decimal("0")


def test_duplicate_material_first_wins_on_price_tie(imported):
    # Обе строки 324816 с ненулевой розницей: ранги равны, побеждает первая.
    part = BrpCatalogPart.objects.get(material_no="324816")
    assert part.part_desc == "SCREW"  # первая строка, не "SCREW DUPLICATE"


# --- Дубликаты Material_No: предпочтение ненулевой цены (hotfix 32.3) -----------------

DUP_417_ROWS = [
    # Реальный кейс: первая строка с нулевой розницей, дубликат с настоящей ценой.
    ["417224916", "ROLLER ZERO", 2020, None, 0, 0, None, None],
    ["417224916", "ROLLER PULLEY EXT", 2025, None, 35.99, 28.15, None, None],
]


def test_duplicate_prefers_nonzero_retail(db, tmp_path):
    summary = import_catalog(_make_xlsx(tmp_path, DUP_417_ROWS), commit=True)
    assert summary.duplicates == 1
    assert summary.duplicates_price_resolved == 1
    part = BrpCatalogPart.objects.get(material_no="417224916")
    assert part.retail_price_usd == Decimal("35.99")
    assert part.wholesale_price_usd == Decimal("28.15")
    assert part.part_desc == "ROLLER PULLEY EXT"
    # 35.99 * 105 * 1.40 = 5290.53 -> 5291 ₽ (целые рубли).
    assert customer_price_rub(part.retail_price_usd, 105, 40) == Decimal("5291")


def test_duplicate_wholesale_tiebreak(db, tmp_path):
    rows = [
        ["555001", "NO PRICES", 2020, None, 0, 0, None, None],
        ["555001", "WHOLESALE ONLY", 2025, None, 0, 12.5, None, None],
    ]
    summary = import_catalog(_make_xlsx(tmp_path, rows), commit=True)
    assert summary.duplicates_price_resolved == 1
    part = BrpCatalogPart.objects.get(material_no="555001")
    assert part.part_desc == "WHOLESALE ONLY"  # розница у обеих 0 -> решает оптовая
    assert part.retail_price_usd == Decimal("0")


def test_duplicate_all_zero_keeps_zero(db, tmp_path):
    rows = [
        ["555002", "ZERO A", 2020, None, 0, 0, None, None],
        ["555002", "ZERO B", 2025, None, 0, 0, None, None],
    ]
    summary = import_catalog(_make_xlsx(tmp_path, rows), commit=True)
    assert summary.duplicates_price_resolved == 0
    part = BrpCatalogPart.objects.get(material_no="555002")
    assert part.part_desc == "ZERO A"  # детерминированно: первая строка
    assert part.retail_price_usd == Decimal("0")


def test_reimport_repairs_zero_price_record(db, tmp_path):
    # В базе запись со старого импорта «первая строка побеждает»: цена 0.
    BrpCatalogPart.objects.create(
        material_no="417224916", part_desc="ROLLER ZERO",
        retail_price_usd=Decimal("0"), wholesale_price_usd=Decimal("0"),
    )
    path = _make_xlsx(tmp_path, DUP_417_ROWS)
    # Dry-run сообщает, что запись будет обновлена, но ничего не пишет.
    dry = import_catalog(path, commit=False)
    assert dry.updated == 1
    assert dry.zero_price_repaired == 1
    assert BrpCatalogPart.objects.get(material_no="417224916").retail_price_usd == Decimal("0")
    # Commit реально чинит запись.
    summary = import_catalog(path, commit=True)
    assert summary.updated == 1
    assert summary.zero_price_repaired == 1
    part = BrpCatalogPart.objects.get(material_no="417224916")
    assert part.retail_price_usd == Decimal("35.99")
    # Повторный импорт стабилен: уже выбранная ненулевая строка не меняется.
    again = import_catalog(path, commit=True)
    assert again.updated == 0
    assert again.skipped_unchanged == 1


def test_brp_search_shows_repaired_price(client, make_user, db, tmp_path):
    import_catalog(_make_xlsx(tmp_path, DUP_417_ROWS), commit=True)
    _login(client, make_user, superuser=True)
    html = client.get(reverse("brp_search") + "?q=417224916").content.decode()
    assert "ROLLER PULLEY EXT" in html
    assert "5291" in html  # не 0: выбрана строка с настоящей розницей


def test_reimport_is_idempotent(db, sample_file):
    import_catalog(sample_file, commit=True)
    summary = import_catalog(sample_file, commit=True)
    assert summary.created == 0
    assert summary.updated == 0
    assert summary.skipped_unchanged == 4
    assert BrpCatalogPart.objects.count() == 4


def test_reimport_updates_changed_rows(db, tmp_path, sample_file):
    import_catalog(sample_file, commit=True)
    changed = [row[:] for row in SAMPLE_ROWS]
    changed[0][4] = 7.99  # новая розница у "042"
    summary = import_catalog(_make_xlsx(tmp_path, changed, "v2.xlsx"), commit=True)
    assert summary.updated == 1
    assert summary.skipped_unchanged == 3
    assert BrpCatalogPart.objects.get(material_no="042").retail_price_usd == Decimal("7.99")


def test_import_never_touches_stock(db, sample_file):
    before = _stock_snapshot()
    import_catalog(sample_file, commit=True)
    assert _stock_snapshot() == before  # ни остатков, ни движений, ни карточек


def test_import_command_output(db, sample_file, capsys):
    call_command("import_brp_catalog", str(sample_file))
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "Создано: 4" in out
    assert BrpCatalogPart.objects.count() == 0
    call_command("import_brp_catalog", str(sample_file), "--commit")
    assert BrpCatalogPart.objects.count() == 4


# --- Цены -----------------------------------------------------------------------


def test_price_rounded_to_whole_rubles():
    """Hotfix 32.1: цена клиента в целых рублях (ROUND_HALF_UP, без копеек)."""
    assert customer_price_rub(Decimal("100"), 105, 40) == Decimal("14700")
    assert customer_price_rub(Decimal("7.39"), 105, 40) == Decimal("1086")  # 1086.33
    assert customer_price_rub(Decimal("9.03"), 105, 40) == Decimal("1327")  # 1327.41
    result = customer_price_rub(Decimal("99.99"), 105, 40)
    assert result == Decimal("14699")  # 14698.53 -> вверх
    assert isinstance(result, Decimal)


def test_price_formula_configurable():
    assert customer_price_rub(Decimal("10"), 90, 25) == Decimal("1125")
    # 10 * 100.5 * 1.125 = 1130.625 -> 1131 (половина копейки вверх).
    assert customer_price_rub(Decimal("10"), Decimal("100.5"), Decimal("12.5")) == (
        Decimal("1131")
    )


def test_price_rounding_keeps_usd_sources(db):
    """Округляется только итог: исходная розница USD не меняется."""
    brp = _brp()
    customer_price_rub(brp.retail_price_usd, 105, 40)
    brp.refresh_from_db()
    assert brp.retail_price_usd == Decimal("99.99")


def test_price_none_without_retail():
    assert customer_price_rub(None, 105, 40) is None
    assert customer_price_rub("", 105, 40) is None


def test_pricing_settings_defaults(db):
    settings = BrpPricingSettings.get()
    assert settings.brp_usd_rate == Decimal("105")
    assert settings.brp_markup_percent == Decimal("40")


# --- Продвижение в склад ------------------------------------------------------------


@pytest.fixture
def refs(db):
    from apps.catalog.models import Unit

    Unit.objects.get(name="Штука")  # сидируется миграцией справочников
    return {}


def _brp(material="219800345", retail="99.99", **kwargs):
    defaults = {
        "part_desc": "BELT DRIVE",
        "retail_price_usd": Decimal(retail) if retail else None,
        "wholesale_price_usd": Decimal("80"),
        "replacement_no_1": "417300571",
        "brp_status": "LIQ",
    }
    defaults.update(kwargs)
    return BrpCatalogPart.objects.create(material_no=material, **defaults)


def test_promote_creates_card_without_stock(db, refs, admin):
    brp = _brp()
    before = _stock_snapshot()
    part = promote_to_warehouse(brp, by=admin)
    assert part.name == "BELT DRIVE"
    assert part.tracking_mode == PartType.TrackingMode.BULK
    assert part.recommended_price == Decimal("14699")  # целые рубли, без копеек
    numbers = set(PartNumber.objects.filter(part=part).values_list("value", flat=True))
    assert numbers == {"219800345", "417300571"}
    link = BrpPartLink.objects.get(part=part)
    assert link.brp_retail_price_usd == Decimal("99.99")
    assert link.usd_rate_used == Decimal("105")
    assert link.markup_percent_used == Decimal("40")
    assert link.calculated_customer_price_rub == Decimal("14699")
    assert link.final_customer_price_rub == Decimal("14699")
    assert link.price_source == BrpPartLink.PriceSource.CALCULATED
    after = _stock_snapshot()
    assert after["balances"] == before["balances"]  # остатков НЕ появилось
    assert after["movements"] == before["movements"]
    assert after["parts"] == before["parts"] + 1  # только карточка


def test_promote_is_idempotent(db, refs, admin):
    brp = _brp()
    first = promote_to_warehouse(brp, by=admin)
    second = promote_to_warehouse(brp, by=admin)
    assert first.pk == second.pk
    assert PartType.objects.count() == 1


def test_manual_price_keeps_sources(db, refs, admin):
    brp = _brp()
    part = promote_to_warehouse(brp, by=admin, manual_price=Decimal("15000"))
    link = part.brp_link
    assert link.price_source == BrpPartLink.PriceSource.MANUAL
    assert link.manual_customer_price_rub == Decimal("15000")
    assert link.final_customer_price_rub == Decimal("15000")
    # Исходники не потеряны: и USD, и рассчитанная цена сохранены.
    assert link.brp_retail_price_usd == Decimal("99.99")
    assert link.calculated_customer_price_rub == Decimal("14699")


def test_settings_change_affects_future_not_past(db, refs, admin):
    old_part = promote_to_warehouse(_brp("111"), by=admin)
    settings = BrpPricingSettings.get()
    settings.brp_usd_rate = Decimal("90")
    settings.brp_markup_percent = Decimal("50")
    settings.save()
    new_part = promote_to_warehouse(_brp("222"), by=admin)
    old_link, new_link = old_part.brp_link, new_part.brp_link
    assert old_link.usd_rate_used == Decimal("105")  # история не изменилась
    assert old_link.calculated_customer_price_rub == Decimal("14699")
    assert new_link.usd_rate_used == Decimal("90")
    # 99.99 * 90 * 1.5 = 13498.65 -> 13499 (целые рубли).
    assert new_link.calculated_customer_price_rub == Decimal("13499")


# --- Поиск -----------------------------------------------------------------------


def _login(client, make_user, *, role=None, superuser=False, name="u"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


def test_brp_search_by_material_and_replacement(client, make_user, imported):
    _login(client, make_user, superuser=True)
    html = client.get(reverse("brp_search") + "?q=042").content.decode()
    assert "BUSHING" in html
    # По замене номера (у 042 замена 460041).
    html = client.get(reverse("brp_search") + "?q=460041").content.decode()
    assert "BUSHING" in html
    # По описанию.
    html = client.get(reverse("brp_search") + "?q=SCREW M6").content.decode()
    assert "353589" in html


def test_search_warehouse_first_then_brp(client, make_user, imported, admin):
    brp = BrpCatalogPart.objects.get(material_no="042")
    part = promote_to_warehouse(brp, by=admin)
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("brp_search") + "?q=042").content.decode()
    assert "На складе" in html  # блок склада присутствует
    assert part.name in html
    # Позиция BRP помечена как уже добавленная (кнопки продвижения нет).
    assert "Создать карточку" not in html


def test_brp_preview_when_not_in_warehouse(client, make_user, imported):
    _login(client, make_user, superuser=True)
    html = client.get(reverse("brp_search") + "?q=353589").content.decode()
    assert "Создать карточку" in html
    assert "Учесть наличие" in html
    assert "LIQ" in html  # статус показан


def test_core_search_shows_brp_fallback(client, make_user, imported):
    _login(client, make_user, superuser=True)
    html = client.get(reverse("part_search") + "?q=353589").content.decode()
    assert "Найдено в BRP-каталоге" in html
    assert "справочник, не остаток" in html


def test_promote_requires_can_manage_parts(client, make_user, imported):
    brp = BrpCatalogPart.objects.get(material_no="042")
    _login(client, make_user, role=roles.STOREKEEPER, name="sklad")
    resp = client.post(reverse("brp_promote", args=[brp.pk]))
    assert resp.status_code == 403


def test_promote_via_view(client, make_user, imported):
    brp = BrpCatalogPart.objects.get(material_no="042")
    _login(client, make_user, superuser=True)
    resp = client.post(reverse("brp_promote", args=[brp.pk]))
    assert resp.status_code == 302
    part = BrpPartLink.objects.get(brp_part=brp).part
    assert resp["Location"] == reverse("part_detail", args=[part.pk])
    assert StockBalance.objects.count() == 0  # остатков нет


# --- Учёт наличия ------------------------------------------------------------------


def test_intake_creates_draft_and_redirects(client, make_user, imported):
    brp = BrpCatalogPart.objects.get(material_no="042")
    _login(client, make_user, superuser=True)
    before = _stock_snapshot()
    resp = client.post(reverse("brp_intake", args=[brp.pk]))
    assert resp.status_code == 302
    draft = Receipt.objects.get()
    part = BrpPartLink.objects.get(brp_part=brp).part
    assert resp["Location"] == f"/receipts/{draft.pk}/?new_part={part.pk}"
    assert draft.status == Receipt.Status.DRAFT
    assert draft.supplier.name == "Стартовый ввод"
    # Черновик не меняет склад.
    assert StockBalance.objects.count() == before["balances"]
    assert StockMovement.objects.count() == before["movements"]
    # Повторный «Учесть наличие» использует тот же черновик.
    client.post(reverse("brp_intake", args=[brp.pk]))
    assert Receipt.objects.count() == 1


def test_posting_intake_creates_stock_at_locations(db, imported, admin):
    brp = BrpCatalogPart.objects.get(material_no="042")
    part = promote_to_warehouse(brp, by=admin)
    loc1 = StorageLocation.objects.create(name="Ячейка", code="A-S01-L02-K03-C01")
    loc2 = StorageLocation.objects.create(name="Ящик", code="B-S02-L01-D01-C04")
    draft = get_or_create_intake_draft(by=admin)
    add_line(draft, part_type=part, quantity=Decimal("2"),
             unit_cost_rub=Decimal("500"), location=loc1)
    add_line(draft, part_type=part, quantity=Decimal("1"),
             unit_cost_rub=Decimal("500"), location=loc2)
    post_receipt(draft, by=admin)
    balances = {
        b.location.code: b.quantity_available
        for b in StockBalance.objects.filter(part_type=part)
    }
    # Одна деталь в двух адресах: остаток привязан к месту.
    assert balances == {
        "A-S01-L02-K03-C01": Decimal("2"),
        "B-S02-L01-D01-C04": Decimal("1"),
    }


def test_storekeeper_intake_needs_promoted_card(client, make_user, imported):
    brp = BrpCatalogPart.objects.get(material_no="042")
    _login(client, make_user, role=roles.STOREKEEPER, name="sklad")
    resp = client.post(reverse("brp_intake", args=[brp.pk]))
    assert resp.status_code == 302  # редирект с сообщением, карточка не создана
    assert not BrpPartLink.objects.exists()
    assert not Receipt.objects.exists()


# --- Настройки цен -------------------------------------------------------------------


def test_settings_page_gated_and_saves(client, make_user, db):
    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.get(reverse("brp_settings")).status_code == 403
    client.logout()
    _login(client, make_user, superuser=True)
    resp = client.post(
        reverse("brp_settings"), {"brp_usd_rate": "98,5", "brp_markup_percent": "35"}
    )
    assert resp.status_code == 302
    settings = BrpPricingSettings.get()
    assert settings.brp_usd_rate == Decimal("98.5")
    assert settings.brp_markup_percent == Decimal("35")


# --- Адресное хранение ---------------------------------------------------------------


def test_address_for_drawer():
    # Новый формат по умолчанию: без зоны.
    assert compose_address("", 1, 2, kind="drawer", unit_no=3, cell_no=8) == "S01-L02-D03-C08"


def test_address_for_box_or_container():
    # Коробка и контейнер — одна буква B (K и X для новых адресов не используются).
    assert compose_address("", 2, 1, kind="container", unit_no=4, cell_no=2) == "S02-L01-B04-C02"
    assert compose_address("", 2, 1, kind="box", unit_no=4, cell_no=2) == "S02-L01-B04-C02"


def test_address_for_open_shelf():
    assert compose_address("", 4, 2) == "S04-L02"
    assert compose_address("", 3, 1, kind="box", unit_no=2) == "S03-L01-B02"


def test_address_legacy_zone_still_works():
    # Старые адреса с зоной остаются собираемыми и валидными.
    assert compose_address("A", 1, 2, kind="drawer", unit_no=1, cell_no=1) == "A-S01-L02-D01-C01"
    assert compose_address("d", 1, 4, kind="shelf") == "D-S01-L04"


def test_address_legacy_codes_remain_searchable(db):
    # Легаси-коды K/X/зоны в базе читаются и переиспользуются как раньше.
    from apps.warehouse.addresses import get_or_create_location

    for code in ("A-S01-L02-D01-C01", "S01-L02-K01-C01", "S01-L02-X01-C01"):
        created = get_or_create_location(code)
        assert get_or_create_location(code).pk == created.pk
        assert StorageLocation.objects.filter(code=code).count() == 1


def test_address_validation():
    with pytest.raises(AddressError):
        compose_address("", 0, 1)
    with pytest.raises(AddressError):
        compose_address("", 1, 1, kind="drawer")  # ящик без номера
    with pytest.raises(AddressError):
        compose_address("", 1, 1, cell_no=5)  # ячейка вне ящика/контейнера


def test_brp_status_legend_visible(client, make_user, db):
    """Hotfix 32.1: легенда статусов BRP видна на странице каталога."""
    _login(client, make_user, superuser=True)
    html = client.get(reverse("brp_search")).content.decode()
    assert "Легенда статусов BRP" in html
    assert "OBS - снято с производства (obsolete)" in html
    assert "USE - была замена номера (use)" in html
    assert "VIN - винтажный склад, будет доставка 25$ (vintage)" in html
    assert "LIQ - последние остатки у завода (liquidation)" in html
    assert "UCP - статус уточняется" in html


def test_brp_price_shown_without_kopecks(client, make_user, imported):
    """Hotfix 32.1: в каталоге цена клиента без копеек (целые рубли)."""
    _login(client, make_user, superuser=True)
    # 353589: 9.03 * 105 * 1.40 = 1327.41 -> 1327.
    html = client.get(reverse("brp_search") + "?q=353589").content.decode()
    assert "1327" in html
    assert "1327,41" not in html and "1327.41" not in html


# --- Гигиена --------------------------------------------------------------------------


def test_brp_pages_have_no_em_dash(client, make_user, imported):
    _login(client, make_user, superuser=True)
    for url in (reverse("brp_search") + "?q=042", reverse("brp_settings")):
        assert "—" not in client.get(url).content.decode()
