"""Layer 32 — быстрая инвентаризация ячейки сканером.

Гарантии: скан и черновик сессии склад НЕ меняют; одинаковые номера
группируются; сырые скан-события хранятся; отмена уменьшает количество;
склад имеет приоритет над BRP; позиции BRP при проведении автоматически
становятся складскими карточками (со снимком цены без округления); остаток
пишется по адресу только при проведении; повторное проведение сессии
запрещено (защита от удвоения).
"""
import io
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.urls import reverse

from apps.accounts import roles
from apps.actions.models import WarehouseAction
from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.counting.models import (
    InventoryCountingLine,
    InventoryCountingSession,
    InventoryScanEvent,
)
from apps.counting.services import (
    CountingError,
    can_delete_session,
    cancel_session,
    convert_to_receipt,
    delete_session,
    find_brp_by_number,
    find_brp_price_source,
    get_session_value_breakdown,
    post_session,
    record_scan,
    refresh_draft_prices,
    remove_line,
    set_line_quantity,
    start_session,
    undo_last_scan,
)
from apps.inventory.models import StockBalance, StockMovement
from apps.receipts.services import add_line, create_receipt, post_receipt, receipt_totals
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import compose_address, get_or_create_location
from apps.warehouse.models import StorageLocation, StorageLocationRenameHistory

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
def refs(db):
    cat = Category.objects.create(name="Крепёж")
    Unit.objects.get(name="Штука")
    # Складская карточка с номером 700700 (для приоритета над BRP).
    wh = PartType.objects.create(
        name="Болт складской", category=cat,
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=wh, value="700700", kind=PartNumber.Kind.OEM)
    # BRP-позиции.
    brp_main = BrpCatalogPart.objects.create(
        material_no="219800345", part_desc="BELT DRIVE",
        retail_price_usd=Decimal("99.99"), wholesale_price_usd=Decimal("80"),
        replacement_no_1="417300571", brp_status="LIQ",
    )
    brp_repl2 = BrpCatalogPart.objects.create(
        material_no="503190", part_desc="SEAL", retail_price_usd=Decimal("10"),
        replacement_no_2="290420",
    )
    # BRP-позиция с тем же номером, что складская карточка -> проверяем приоритет.
    brp_dup = BrpCatalogPart.objects.create(material_no="700700", part_desc="BRP BOLT")
    return {"wh": wh, "brp_main": brp_main, "brp_repl2": brp_repl2, "brp_dup": brp_dup}


@pytest.fixture
def location(db):
    return get_or_create_location("B-S01-L02-D03-C08", name="Ящик 3 ячейка 8")


def _stock_snapshot():
    return {
        "balances": StockBalance.objects.count(),
        "movements": StockMovement.objects.count(),
        "parts": PartType.objects.count(),
    }


# --- Адрес / место хранения ---------------------------------------------------------


def test_address_composed_and_reused(db):
    # Новый формат без зоны (по умолчанию) и легаси-формат с зоной.
    assert compose_address("", 1, 2, kind="drawer", unit_no=3, cell_no=8) == "S01-L02-D03-C08"
    assert compose_address("B", 1, 2, kind="drawer", unit_no=3, cell_no=8) == "B-S01-L02-D03-C08"
    loc1 = get_or_create_location("B-S01-L02-D03-C08")
    loc2 = get_or_create_location("B-S01-L02-D03-C08")
    assert loc1.pk == loc2.pk  # дубликат кода не создаётся
    assert StorageLocation.objects.filter(code="B-S01-L02-D03-C08").count() == 1


def test_start_session_snapshots_address(refs, location, admin):
    session = start_session(location=location, by=admin)
    assert session.full_address == "B-S01-L02-D03-C08"
    assert session.status == InventoryCountingSession.Status.DRAFT
    assert session.storage_location == location


# --- Сканирование и группировка -----------------------------------------------------


def test_scan_creates_event_and_grouped_line(refs, location, admin):
    session = start_session(location=location, by=admin)
    line = record_scan(session, "219800345", by=admin)
    assert line.quantity_counted == Decimal("1")
    assert line.scan_count == 1
    assert line.scanned_value == "219800345"  # строкой
    assert InventoryScanEvent.objects.filter(session=session).count() == 1
    assert session.lines.count() == 1


def test_duplicate_scan_increments_same_line(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    record_scan(session, "219 800 345", by=admin)  # тот же номер, иные пробелы
    assert session.lines.count() == 1
    line = session.lines.get()
    assert line.quantity_counted == Decimal("2")
    assert line.scan_count == 2
    assert InventoryScanEvent.objects.filter(session=session).count() == 2  # оба события целы


def test_undo_decrements_then_removes(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    record_scan(session, "219800345", by=admin)
    assert undo_last_scan(session) is True
    line = session.lines.get()
    assert line.quantity_counted == Decimal("1")
    assert undo_last_scan(session) is True
    assert session.lines.count() == 0  # строка удалена при нуле
    assert undo_last_scan(session) is False  # отменять больше нечего


# --- Сопоставление ------------------------------------------------------------------


def test_warehouse_match_has_priority(refs, location, admin):
    session = start_session(location=location, by=admin)
    line = record_scan(session, "700700", by=admin)  # есть и на складе, и в BRP
    assert line.source == "warehouse"
    assert line.warehouse_part == refs["wh"]


def test_brp_material_and_replacement_match(refs, location, admin):
    session = start_session(location=location, by=admin)
    m = record_scan(session, "219800345", by=admin)
    assert m.source == "brp_catalog" and m.brp_catalog_part == refs["brp_main"]
    r1 = record_scan(session, "417300571", by=admin)  # replacement_no_1
    assert r1.brp_catalog_part == refs["brp_main"]
    r2 = record_scan(session, "290420", by=admin)  # replacement_no_2
    assert r2.brp_catalog_part == refs["brp_repl2"]
    assert m.final_customer_price_rub == Decimal("14699")  # 14698.53 -> целые рубли


def test_unknown_line_created(refs, location, admin):
    session = start_session(location=location, by=admin)
    line = record_scan(session, "NO-SUCH-999", by=admin)
    assert line.source == "unknown"
    assert line.display_name == "Неизвестная деталь"
    assert line.needs_review is True


# --- Черновик не трогает склад; авто-создание карточек из BRP ------------------------


def test_scan_and_convert_do_not_change_stock(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    record_scan(session, "700700", by=admin)
    before = _stock_snapshot()
    convert_to_receipt(session, by=admin)  # создаёт карточку из BRP, но не остаток
    after = _stock_snapshot()
    assert after["balances"] == before["balances"]
    assert after["movements"] == before["movements"]
    assert after["parts"] == before["parts"] + 1  # только карточка из BRP


def test_convert_autocreates_brp_card_with_price_snapshot(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    convert_to_receipt(session, by=admin)
    link = BrpPartLink.objects.get(brp_part=refs["brp_main"])
    assert link.usd_rate_used == Decimal("105")
    assert link.markup_percent_used == Decimal("40")
    assert link.calculated_customer_price_rub == Decimal("14699")  # целые рубли
    line = session.lines.get()
    assert line.source == "warehouse"  # после конвертации привязана к складу
    assert line.warehouse_part == link.part


def test_convert_reuses_existing_promoted_card(refs, location, admin):
    from apps.brp.services import promote_to_warehouse

    existing = promote_to_warehouse(refs["brp_main"], by=admin)
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    convert_to_receipt(session, by=admin)
    assert BrpPartLink.objects.filter(brp_part=refs["brp_main"]).count() == 1
    assert session.lines.get().warehouse_part == existing


def test_unknown_blocks_convert(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "WHAT-IS-THIS", by=admin)
    with pytest.raises(CountingError):
        convert_to_receipt(session, by=admin)


# --- Проведение пишет остаток по адресу ---------------------------------------------


def test_posting_creates_stock_at_location(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    record_scan(session, "700700", by=admin)
    record_scan(session, "700700", by=admin)
    before = _stock_snapshot()
    convert_to_receipt(session, by=admin)
    assert _stock_snapshot()["balances"] == before["balances"]  # черновик не тронул склад
    post_session(session, by=admin)
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.POSTED
    balance = StockBalance.objects.get(part_type=refs["wh"], location=location)
    assert balance.quantity_available == Decimal("3")


def test_double_post_is_blocked(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    before = _stock_snapshot()
    with pytest.raises(CountingError):
        post_session(session, by=admin)  # повторное проведение удвоило бы остаток
    assert _stock_snapshot() == before


def test_same_part_two_locations(refs, admin):
    loc1 = get_or_create_location("B-S01-L02-D03-C08")
    loc2 = get_or_create_location("A-S02-L01-K04-C02")
    s1 = start_session(location=loc1, by=admin)
    record_scan(s1, "219800345", by=admin)
    record_scan(s1, "219800345", by=admin)
    post_session(s1, by=admin)
    s2 = start_session(location=loc2, by=admin)
    record_scan(s2, "219800345", by=admin)
    post_session(s2, by=admin)
    part = BrpPartLink.objects.get(brp_part=refs["brp_main"]).part
    balances = {
        b.location.code: b.quantity_available
        for b in StockBalance.objects.filter(part_type=part)
    }
    assert balances == {"B-S01-L02-D03-C08": Decimal("2"), "A-S02-L01-K04-C02": Decimal("1")}


def test_cancel_session(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    cancel_session(session)
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.CANCELLED
    with pytest.raises(CountingError):
        record_scan(session, "700700", by=admin)  # завершённая сессия не сканируется


# --- Экраны и доступ ----------------------------------------------------------------


def _login(client, make_user, *, role=None, superuser=False, name="u"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


def test_list_and_gating(client, make_user):
    _login(client, make_user, role=roles.STOREKEEPER, name="sklad")
    assert client.get(reverse("counting_list")).status_code == 200
    client.logout()
    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.get(reverse("counting_list")).status_code == 403


def test_counting_rename_button_is_visible_for_manager_in_draft_and_posted(
    client, make_user, refs, location, admin
):
    draft = start_session(location=location, by=admin)
    posted = start_session(location=location, by=admin)
    record_scan(posted, "700700", by=admin)
    post_session(posted, by=admin)
    _login(client, make_user, role=roles.MANAGER, name="manager")

    for session in (draft, posted):
        html = client.get(reverse("counting_detail", args=[session.pk])).content.decode()
        rename_url = reverse("location_rename", args=[location.pk])
        detail_url = reverse("counting_detail", args=[session.pk])
        assert "Переименовать ячейку" in html
        assert f"{rename_url}?next={detail_url}" in html


def test_counting_rename_button_is_hidden_without_warehouse_structure_permission(
    client, make_user, location, admin
):
    session = start_session(location=location, by=admin)
    _login(client, make_user, role=roles.STOREKEEPER, name="storekeeper")
    response = client.get(reverse("counting_detail", args=[session.pk]))
    assert response.status_code == 200
    assert "Переименовать ячейку" not in response.content.decode()


def test_counting_rename_returns_to_same_posted_session_without_mutating_inventory(
    client, make_user, refs, location, admin
):
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    session.refresh_from_db()
    session_pk = session.pk
    old_code = location.code
    action = WarehouseAction.objects.create(
        action_type=WarehouseAction.Type.SALE,
        part_type=refs["wh"],
        part_number="700700",
        part_name=refs["wh"].name,
        location=location,
        location_code=old_code,
        quantity=Decimal("1"),
        customer_comment="Проверка snapshot",
    )
    detail_url = reverse("counting_detail", args=[session.pk])
    rename_url = reverse("location_rename", args=[location.pk])
    scans_before = InventoryScanEvent.objects.filter(session=session).count()
    counters_before = session.counters()
    stock_before = _stock_snapshot()
    lines_before = list(
        session.lines.values_list(
            "scanned_value",
            "warehouse_part_id",
            "quantity_counted",
            "final_customer_price_rub",
        )
    )

    client.login(username="admin", password=PASSWORD)
    page = client.get(detail_url)
    assert f"{rename_url}?next={detail_url}" in page.content.decode()
    response = client.post(
        rename_url,
        {
            "expected_code": old_code,
            "new_code": "S04-L03-D02-C08",
            "next": detail_url,
        },
    )
    assert response.status_code == 302
    assert response["Location"] == detail_url

    location.refresh_from_db()
    session.refresh_from_db()
    action.refresh_from_db()
    assert location.code == "S04-L03-D02-C08"
    assert session.pk == session_pk
    assert session.full_address == old_code
    assert session.status == InventoryCountingSession.Status.POSTED
    assert InventoryScanEvent.objects.filter(session=session).count() == scans_before
    assert session.counters() == counters_before
    assert _stock_snapshot() == stock_before
    assert list(
        session.lines.values_list(
            "scanned_value",
            "warehouse_part_id",
            "quantity_counted",
            "final_customer_price_rub",
        )
    ) == lines_before
    assert action.location_code == old_code
    assert action.location.code == location.code
    assert StorageLocationRenameHistory.objects.filter(
        location=location,
        old_code=old_code,
        new_code=location.code,
    ).count() == 1

    returned = client.get(detail_url)
    assert returned.status_code == 200
    assert location.code in returned.content.decode()


def test_scan_endpoint_enter_handling(client, make_user, refs, location):
    _login(client, make_user, superuser=True, name="boss")
    session = InventoryCountingSession.objects.create(
        storage_location=location, full_address=location.code, title="t",
    )
    resp = client.post(reverse("counting_scan", args=[session.pk]), {"code": "219800345"})
    assert resp.status_code == 302  # PRG: возврат на страницу сканера
    assert session.lines.count() == 1
    assert session.lines.get().brp_catalog_part == refs["brp_main"]


def test_new_session_creates_location_without_zone(client, make_user, refs):
    """Hotfix 32.1: зона не требуется, адрес собирается без неё, коробка = B."""
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(
        reverse("counting_new"),
        {
            "rack_number": "1", "level_number": "1",
            "place_type": "box", "place_number": "1", "cell_number": "3", "comment": "",
        },
    )
    assert resp.status_code == 302
    assert StorageLocation.objects.filter(code="S01-L01-B01-C03").exists()


def test_new_page_shows_address_legend(client, make_user, db):
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("counting_new")).content.decode()
    assert "Легенда адреса склада" in html
    assert "S - стеллаж (shelving unit)" in html
    assert "L - уровень, снизу вверх (level)" in html
    assert "D - выдвижной ящик (drawer)" in html
    assert "B - коробка или контейнер (box/bin)" in html
    assert "C - ячейка внутри ящика или контейнера (cell/compartment)" in html
    assert "S01-L02-D03-C08" in html
    assert "S02-L01-B04-C02" in html
    assert "S04-L02" in html


def test_comment_editable_and_safe(client, make_user, refs, location):
    """Hotfix 32.1: описание ячейки редактируется и не трогает ни склад, ни сканы."""
    _login(client, make_user, superuser=True, name="boss")
    session = InventoryCountingSession.objects.create(
        storage_location=location, full_address=location.code, title="t",
    )
    record_scan(session, "700700")
    before = _stock_snapshot()
    events_before = InventoryScanEvent.objects.filter(session=session).count()
    resp = client.post(
        reverse("counting_comment", args=[session.pk]),
        {"comment": "Роллеры вариатора, отсортированы после пересчёта"},
    )
    assert resp.status_code == 302
    session.refresh_from_db()
    assert session.comment == "Роллеры вариатора, отсортированы после пересчёта"
    assert _stock_snapshot() == before  # склад не тронут
    assert InventoryScanEvent.objects.filter(session=session).count() == events_before
    assert session.lines.get().scan_count == 1  # количество не изменилось
    # Видно на странице сессии и в списке.
    assert "Роллеры вариатора" in client.get(
        reverse("counting_detail", args=[session.pk])
    ).content.decode()
    assert "Роллеры вариатора" in client.get(reverse("counting_list")).content.decode()


def test_comment_editable_after_posting(client, make_user, refs, location, admin):
    """Описание — метаданные: правится и после проведения, остатки не меняются."""
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    before = _stock_snapshot()
    client.post(reverse("counting_comment", args=[session.pk]), {"comment": "Подшипники"})
    session.refresh_from_db()
    assert session.comment == "Подшипники"
    assert session.status == InventoryCountingSession.Status.POSTED
    assert _stock_snapshot() == before


def test_full_flow_via_views(client, make_user, refs, location):
    _login(client, make_user, superuser=True, name="boss")
    session = InventoryCountingSession.objects.create(
        storage_location=location, full_address=location.code, title="t",
    )
    client.post(reverse("counting_scan", args=[session.pk]), {"code": "700700"})
    client.post(reverse("counting_scan", args=[session.pk]), {"code": "700700"})
    # convert (создать черновик документа)
    client.post(reverse("counting_convert", args=[session.pk]), {"unit_cost": "0"})
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.CONVERTED
    # post
    resp = client.post(reverse("counting_post", args=[session.pk]))
    assert resp.status_code == 302
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.POSTED
    balance = StockBalance.objects.get(part_type=refs["wh"], location=location)
    assert balance.quantity_available == Decimal("2")


# --- Итоги ячейки: Деталей и Стоимость (hotfix 32.3) --------------------------------


def test_empty_draft_totals_are_zero(refs, location, admin):
    session = start_session(location=location, by=admin)
    c = session.counters()
    assert c["total_quantity"] == Decimal("0")
    assert c["total_value"] == Decimal("0")


def test_totals_follow_scans_and_manual_quantity(refs, location, admin):
    session = start_session(location=location, by=admin)
    line = record_scan(session, "219800345", by=admin)  # цена клиента 14699
    c = session.counters()
    assert c["total_quantity"] == Decimal("1")
    assert c["total_value"] == Decimal("14699")
    record_scan(session, "219800345", by=admin)
    c = session.counters()
    assert c["total_quantity"] == Decimal("2")
    assert c["total_value"] == Decimal("29398")
    # Отпикали одну заводскую упаковку и вручную поставили 13 штук.
    line.refresh_from_db()
    set_line_quantity(line, 13)
    c = session.counters()
    assert c["total_scans"] == 2  # сканы остаются сырыми сканами
    assert c["unique"] == 1  # позиций
    assert c["total_quantity"] == Decimal("13")  # деталей
    assert c["total_value"] == Decimal("13") * Decimal("14699")  # 191087


def test_totals_sum_multiple_lines_and_line_removal(refs, location, admin):
    session = start_session(location=location, by=admin)
    line1 = record_scan(session, "219800345", by=admin)  # 14699
    line2 = record_scan(session, "503190", by=admin)  # 10 * 105 * 1.4 = 1470
    record_scan(session, "NO-SUCH-999", by=admin)  # неизвестная: цены нет
    set_line_quantity(line1, 13)
    c = session.counters()
    assert c["total_quantity"] == Decimal("15")  # 13 + 1 + 1
    assert c["total_value"] == Decimal("13") * Decimal("14699") + Decimal("1470")
    remove_line(line2)
    c = session.counters()
    assert c["total_quantity"] == Decimal("14")
    assert c["total_value"] == Decimal("13") * Decimal("14699")


def test_spec_example_5291_times_13(refs, location, admin):
    BrpCatalogPart.objects.create(
        material_no="417224916", part_desc="ROLLER PULLEY EXT",
        retail_price_usd=Decimal("35.99"), wholesale_price_usd=Decimal("28.15"),
    )
    session = start_session(location=location, by=admin)
    line = record_scan(session, "417224916", by=admin)
    assert line.final_customer_price_rub == Decimal("5291")  # 5290.53 -> 5291
    set_line_quantity(line, 13)
    assert session.counters()["total_value"] == Decimal("68783")  # 13 * 5291


def test_detail_page_shows_totals_without_warehouse_tile(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    line = record_scan(session, "219800345", by=admin)
    # Ручная правка количества через форму строки: редирект обратно.
    resp = client.post(
        reverse("counting_line_qty", args=[line.pk]), {"quantity": "13"}, follow=True
    )
    html = resp.content.decode()
    assert "Итоговое количество" in html  # Layer 34: переименовано
    assert "Стоимость ячейки" in html
    assert "191 087" in html  # 13 * 14699: итог обновился после правки
    assert "13" in html
    # «Найдено в складе» убрано из сводки, источник в таблице строк остался.
    assert "Найдено в складе" not in html
    assert "BRP-каталог" in html
    assert "Найдено в BRP" in html


def test_list_page_shows_details_and_value_columns(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    line = record_scan(session, "219800345", by=admin)
    set_line_quantity(line, 13)
    html = client.get(reverse("counting_list")).content.decode()
    assert "Деталей" in html
    assert "Стоимость" in html
    assert "191 087" in html
    assert "Сканов" in html  # сырые сканы отдельной колонкой


# --- Точный номер BRP выше совпадения по замене (hotfix 32.3.1) ----------------------


def _make_old_replaced_417(**overrides):
    """Старый номер 417224458 (розница 0, USE), замена указывает на 417224916."""
    defaults = dict(
        material_no="417224458", part_desc="ROLLER PULLEY EXT",
        retail_price_usd=Decimal("0"), wholesale_price_usd=Decimal("0"),
        brp_status="USE", replacement_no_1="417224916",
    )
    defaults.update(overrides)
    return BrpCatalogPart.objects.create(**defaults)


def _make_exact_417(**overrides):
    """Настоящая позиция 417224916 с реальной ценой (35.99 $ -> 5291 ₽)."""
    defaults = dict(
        material_no="417224916", part_desc="ROLLER_PULLEY EXT",
        retail_price_usd=Decimal("35.99"), wholesale_price_usd=Decimal("28.15"),
        replacement_no_1="417224458",
    )
    defaults.update(overrides)
    return BrpCatalogPart.objects.create(**defaults)


def test_lookup_exact_material_outranks_replacement(db):
    # Старый номер создаётся ПЕРВЫМ (меньший pk), как в продакшен-базе.
    _make_old_replaced_417()
    exact = _make_exact_417()
    found = find_brp_by_number("417224916")
    assert found == exact
    assert found.material_no == "417224916"  # не 417224458


def test_lookup_exact_wins_even_with_zero_price(db):
    # Точный номер ВСЕГДА выше замены, даже если у точного розница 0.
    _make_old_replaced_417(
        material_no="417000001", retail_price_usd=Decimal("99"),
        replacement_no_1="417000002",
    )
    exact_zero = BrpCatalogPart.objects.create(
        material_no="417000002", part_desc="EXACT ZERO", retail_price_usd=Decimal("0"),
    )
    assert find_brp_by_number("417000002") == exact_zero


def test_replacement_fallback_without_exact_match(refs, location, admin):
    # Точной позиции 417224916 нет: скан находит старый номер по замене.
    old = _make_old_replaced_417()
    session = start_session(location=location, by=admin)
    line = record_scan(session, "417224916", by=admin)
    assert line.brp_catalog_part == old
    assert line.final_customer_price_rub == Decimal("0")


def test_scan_exact_outranks_replacement(refs, location, admin):
    _make_old_replaced_417()
    exact = _make_exact_417()
    session = start_session(location=location, by=admin)
    line = record_scan(session, "417224916", by=admin)
    assert line.brp_catalog_part == exact
    assert line.final_customer_price_rub == Decimal("5291")  # 35.99*105*1.4


def test_refresh_relinks_wrong_draft_line(refs, location, admin):
    """Продакшен-кейс: строка черновика привязана к 417224458 с ценой 0.

    После появления точной позиции 417224916 в каталоге refresh перепривязывает
    строку и обновляет название/цену; количество и сканы не меняются.
    """
    old = _make_old_replaced_417()
    session = start_session(location=location, by=admin)
    record_scan(session, "417224916", by=admin)
    record_scan(session, "417224916", by=admin)
    record_scan(session, "417224916", by=admin)  # 3 скана, как в проде
    line = session.lines.get()
    assert line.brp_catalog_part == old and line.final_customer_price_rub == Decimal("0")
    exact = _make_exact_417()  # реимпорт добавил настоящую позицию
    before = _stock_snapshot()
    assert refresh_draft_prices(session) == 1
    line.refresh_from_db()
    assert line.brp_catalog_part == exact  # перепривязана
    assert line.display_name == "ROLLER_PULLEY EXT"
    assert line.final_customer_price_rub == Decimal("5291")
    assert line.quantity_counted == Decimal("3")  # количество не тронуто
    assert line.scan_count == 3  # сканы не тронуты
    assert session.counters()["total_value"] == Decimal("15873")  # 3 * 5291
    assert _stock_snapshot() == before  # склад не изменился


def test_refresh_does_not_relink_posted_session(refs, location, admin):
    old = _make_old_replaced_417()
    session = start_session(location=location, by=admin)
    record_scan(session, "417224916", by=admin)
    post_session(session, by=admin)
    _make_exact_417()
    before = _stock_snapshot()
    assert refresh_draft_prices(session) == 0
    line = session.lines.get()
    assert line.brp_catalog_part == old  # история не переписана
    assert line.final_customer_price_rub == Decimal("0")
    assert _stock_snapshot() == before


# --- Источник цены: замена с ценой при нулевом точном номере (hotfix 32.3.2) --------


def _make_zero_screw_059():
    """Точная позиция 250000059 без цены (как в проде, source_row 9041)."""
    return BrpCatalogPart.objects.create(
        material_no="250000059", part_desc="HEX. FLANGED SCEW M6 X 18",
        retail_price_usd=Decimal("0"), wholesale_price_usd=Decimal("0"),
    )


def _make_priced_screw_418():
    """Связанная позиция 250000418 с ценой; её замена указывает на 250000059."""
    return BrpCatalogPart.objects.create(
        material_no="250000418", part_desc="FLANGED HEX. SCREW M6 X 18, SCOTCH GRIP",
        retail_price_usd=Decimal("4.19"), wholesale_price_usd=Decimal("3.29"),
        replacement_no_1="250000059",
    )


def test_scan_zero_exact_uses_priced_replacement_as_price_source(refs, location, admin):
    """Case A: личность строки — точный номер, цена — от замены с розницей."""
    exact = _make_zero_screw_059()
    priced = _make_priced_screw_418()
    assert find_brp_price_source("250000059", exact) == priced
    session = start_session(location=location, by=admin)
    line = record_scan(session, "250000059", by=admin)
    assert line.brp_catalog_part == exact  # личность НЕ перепривязана
    assert line.brp_catalog_part.material_no == "250000059"
    assert line.final_customer_price_rub == Decimal("616")  # 4.19*105*1.4=615.93


def test_refresh_fixes_zero_price_keeping_identity(refs, location, admin):
    """Case A для существующего черновика: перескан не нужен."""
    exact = _make_zero_screw_059()
    session = start_session(location=location, by=admin)
    record_scan(session, "250000059", by=admin)
    line = session.lines.get()
    assert line.final_customer_price_rub == Decimal("0")  # цены ещё нет
    _make_priced_screw_418()  # реимпорт добавил связанную позицию с ценой
    before = _stock_snapshot()
    assert refresh_draft_prices(session) == 1
    line.refresh_from_db()
    assert line.brp_catalog_part == exact  # осталась 250000059
    assert line.final_customer_price_rub == Decimal("616")
    assert line.quantity_counted == Decimal("1")
    assert line.scan_count == 1
    assert session.counters()["total_value"] == Decimal("616")  # итог включает 616
    assert _stock_snapshot() == before


def test_exact_nonzero_price_source_is_itself(refs, location, admin):
    """Case B: у точного номера есть цена — нулевая замена не используется."""
    _make_old_replaced_417()  # 417224458, розница 0
    exact = _make_exact_417()  # 417224916, розница 35.99
    assert find_brp_price_source("417224916", exact) == exact
    session = start_session(location=location, by=admin)
    line = record_scan(session, "417224916", by=admin)
    assert line.brp_catalog_part == exact
    assert line.final_customer_price_rub == Decimal("5291")


def test_all_related_zero_keeps_zero_price(refs, location, admin):
    """Case C: ни у кого из цепочки нет цены — остаётся 0, без падений."""
    exact = _make_zero_screw_059()
    BrpCatalogPart.objects.create(
        material_no="250000777", part_desc="ZERO RELATIVE",
        retail_price_usd=Decimal("0"), replacement_no_1="250000059",
    )
    assert find_brp_price_source("250000059", exact) == exact
    session = start_session(location=location, by=admin)
    line = record_scan(session, "250000059", by=admin)
    assert line.final_customer_price_rub == Decimal("0")
    assert refresh_draft_prices(session) == 0  # ничего не меняется, стабильно


def test_forward_replacement_of_selected_part_is_price_source(refs, location, admin):
    """Замены самой позиции тоже кандидаты: старый номер 0 ссылается на новый с ценой."""
    old = BrpCatalogPart.objects.create(
        material_no="333000111", part_desc="OLD ZERO", brp_status="USE",
        retail_price_usd=Decimal("0"), replacement_no_1="333000222",
    )
    new = BrpCatalogPart.objects.create(
        material_no="333000222", part_desc="NEW PRICED", retail_price_usd=Decimal("10"),
    )
    session = start_session(location=location, by=admin)
    line = record_scan(session, "333000111", by=admin)  # точный номер: старый
    assert line.brp_catalog_part == old  # личность — отсканированный номер
    assert find_brp_price_source("333000111", old) == new
    assert line.final_customer_price_rub == Decimal("1470")  # 10*105*1.4


def test_posted_zero_price_not_touched_by_price_source(refs, location, admin):
    _make_zero_screw_059()
    session = start_session(location=location, by=admin)
    record_scan(session, "250000059", by=admin)
    post_session(session, by=admin)
    _make_priced_screw_418()
    before = _stock_snapshot()
    assert refresh_draft_prices(session) == 0
    line = session.lines.get()
    assert line.final_customer_price_rub == Decimal("0")  # история не переписана
    assert _stock_snapshot() == before


# --- Разбор стоимости ячейки и эффективные цены при конвертации (Layer 32.4) --------


def _line(session, number, qty, price, **overrides):
    defaults = dict(
        session=session, scanned_value=number, normalized_value=number,
        display_name=f"PART {number}", source=InventoryCountingLine.Source.BRP,
        quantity_counted=Decimal(str(qty)), scan_count=1,
        final_customer_price_rub=Decimal(str(price)) if price is not None else None,
    )
    defaults.update(overrides)
    return InventoryCountingLine.objects.create(**defaults)


def test_value_breakdown_helper_math(refs, location, admin):
    session = start_session(location=location, by=admin)
    _line(session, "417127016", 13, 3821)
    _line(session, "100200300", 19, 513)
    _line(session, "100200301", 1, 2571)
    data = get_session_value_breakdown(session)
    totals = {row["number"]: row["line_total_rub"] for row in data["rows"]}
    assert totals["417127016"] == Decimal("49673")  # 13 x 3821
    assert totals["100200300"] == Decimal("9747")  # 19 x 513
    assert totals["100200301"] == Decimal("2571")
    assert data["total_value_rub"] == Decimal("61991")  # сумма строк
    assert data["total_quantity"] == Decimal("33")  # 13 + 19 + 1
    assert data["positions_count"] == 3
    # Разбор и счётчики согласованы: без расхождений между плиткой и модалкой.
    assert data["total_value_rub"] == session.counters()["total_value"]


def test_detail_page_value_breakdown_modal(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)  # 14699
    before = _stock_snapshot()
    html = client.get(reverse("counting_detail", args=[session.pk])).content.decode()
    assert _stock_snapshot() == before  # открытие страницы/модалки склад не меняет
    # Плитка кликабельна и ведёт на модалку.
    assert 'href="#value-breakdown"' in html
    assert "Нажмите для расчёта" in html
    # Модалка: заголовок, пояснение, ячейка, сводка.
    assert "Расчёт стоимости ячейки" in html
    assert "Стоимость считается как количество × цена клиента" in html
    assert "Всего позиций: 1" in html
    assert "Итоговая стоимость: 14 699 ₽" in html
    # Колонки таблицы разбора.
    for col in ("Номер", "Название", "Источник", "Кол-во", "Цена клиента", "Расчёт", "Сумма"):
        assert col in html
    # Расчёт строки и итог; итог модалки равен значению плитки.
    assert "1 × 14 699 ₽" in html
    assert "Итого: 14 699 ₽" in html
    assert html.count("14 699 ₽") >= 3  # плитка + строка + итог
    assert "Закрыть" in html


def test_manual_quantity_updates_card_and_modal(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    line = record_scan(session, "219800345", by=admin)
    resp = client.post(
        reverse("counting_line_qty", args=[line.pk]), {"quantity": "13"}, follow=True
    )
    html = resp.content.decode()
    assert html.count("191 087 ₽") >= 3  # плитка, сумма строки, итог модалки
    assert "13 × 14 699 ₽" in html  # расчёт строки


def test_zero_price_row_visible_in_breakdown(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)  # 14699
    record_scan(session, "NO-SUCH-999", by=admin)  # неизвестная, цены нет
    data = get_session_value_breakdown(session)
    assert data["positions_count"] == 2  # нулевая строка НЕ скрыта
    zero_row = next(r for r in data["rows"] if r["number"] == "NO-SUCH-999")
    assert zero_row["line_total_rub"] == Decimal("0")
    assert zero_row["source_label"] == "Требует разбора"
    assert data["total_value_rub"] == Decimal("14699")  # нулевая даёт 0
    html = client.get(reverse("counting_detail", args=[session.pk])).content.decode()
    assert "цена 0" in html


def test_convert_promotes_with_effective_price_616(refs, location, admin):
    """Case 250000059: карточка получает 616 ₽ (цена из замены), а не 0."""
    exact = _make_zero_screw_059()
    _make_priced_screw_418()
    session = start_session(location=location, by=admin)
    record_scan(session, "250000059", by=admin)
    line = session.lines.get()
    assert line.final_customer_price_rub == Decimal("616")
    convert_to_receipt(session, by=admin)
    link = BrpPartLink.objects.get(brp_part=exact)  # личность: 250000059
    assert link.final_customer_price_rub == Decimal("616")  # не 0
    assert link.manual_customer_price_rub == Decimal("616")  # эффективная цена
    assert link.part.recommended_price == Decimal("616")
    # Карточка представляет отсканированный номер, не источник цены.
    assert link.part.name == "HEX. FLANGED SCEW M6 X 18"
    numbers = set(PartNumber.objects.filter(part=link.part).values_list("value", flat=True))
    assert "250000059" in numbers
    assert not BrpPartLink.objects.filter(brp_part__material_no="250000418").exists()
    # Согласованность: цена в пересчёте равна цене снимка.
    line.refresh_from_db()
    assert line.final_customer_price_rub == link.final_customer_price_rub


def test_convert_promotes_exact_nonzero_as_calculated(refs, location, admin):
    """Case 417224916: обычный расчёт, без manual-переопределения."""
    _make_old_replaced_417()
    exact = _make_exact_417()
    session = start_session(location=location, by=admin)
    record_scan(session, "417224916", by=admin)
    convert_to_receipt(session, by=admin)
    link = BrpPartLink.objects.get(brp_part=exact)
    assert link.final_customer_price_rub == Decimal("5291")
    assert link.price_source == BrpPartLink.PriceSource.CALCULATED
    assert link.manual_customer_price_rub is None
    assert link.part.recommended_price == Decimal("5291")


def test_convert_all_zero_promotes_zero(refs, location, admin):
    exact = _make_zero_screw_059()  # связанных с ценой нет
    session = start_session(location=location, by=admin)
    record_scan(session, "250000059", by=admin)
    convert_to_receipt(session, by=admin)  # не падает
    link = BrpPartLink.objects.get(brp_part=exact)
    assert link.final_customer_price_rub == Decimal("0")


# --- Сортировка разбора стоимости (Layer 32.4.1) ------------------------------------


def _sorted_session(location, admin):
    """Четыре строки с разными суммами; порядок создания отличается от сумм."""
    session = start_session(location=location, by=admin)
    _line(session, "100", 19, 513)  # сумма 9747
    _line(session, "050", 5, 0)  # сумма 0 (нулевая цена)
    _line(session, "300", 13, 3821)  # сумма 49673
    _line(session, "200", 1, 2571)  # сумма 2571
    return session


def _numbers(session, sort):
    return [row["number"] for row in get_session_value_breakdown(session, sort=sort)["rows"]]


def test_breakdown_sort_modes(refs, location, admin):
    session = _sorted_session(location, admin)
    assert _numbers(session, "sum_desc") == ["300", "100", "200", "050"]
    assert _numbers(session, "sum_asc") == ["050", "200", "100", "300"]
    assert _numbers(session, "qty_desc") == ["100", "300", "050", "200"]
    assert _numbers(session, "qty_asc") == ["200", "050", "300", "100"]
    assert _numbers(session, "price_desc") == ["300", "200", "100", "050"]
    assert _numbers(session, "price_asc") == ["050", "100", "200", "300"]
    assert _numbers(session, "number_asc") == ["050", "100", "200", "300"]
    assert _numbers(session, "number_desc") == ["300", "200", "100", "050"]
    # «Как в инвентаризации» = порядок таблицы пересчёта; обратный = наоборот.
    original = [line.scanned_value for line in session.lines.all()]
    assert _numbers(session, "original") == original
    assert _numbers(session, "original_desc") == list(reversed(original))


def test_breakdown_default_and_invalid_sort(refs, location, admin):
    session = _sorted_session(location, admin)
    default = get_session_value_breakdown(session)
    assert default["sort"] == "sum_desc"
    assert [r["number"] for r in default["rows"]] == ["300", "100", "200", "050"]
    invalid = get_session_value_breakdown(session, sort="banana")
    assert invalid["sort"] == "sum_desc"  # откат к умолчанию
    assert [r["number"] for r in invalid["rows"]] == ["300", "100", "200", "050"]


def test_breakdown_sort_does_not_change_totals(refs, location, admin):
    session = _sorted_session(location, admin)
    from apps.counting.services import VALUE_SORTS

    for sort in VALUE_SORTS:
        data = get_session_value_breakdown(session, sort=sort)
        assert data["total_value_rub"] == Decimal("61991"), sort
        assert data["total_quantity"] == Decimal("38"), sort
        assert data["positions_count"] == 4, sort
        # Нулевая строка видна в каждом режиме.
        assert any(r["number"] == "050" for r in data["rows"]), sort


def test_breakdown_sort_via_view_keeps_main_table_order(client, make_user, refs, location, admin):
    import re

    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    _line(session, "9111", 1, 100)  # сумма 100 (создана первой)
    _line(session, "9222", 1, 200)  # сумма 200
    url = reverse("counting_detail", args=[session.pk])
    html = client.get(url + "?value_sort=sum_desc").content.decode()
    main_part, modal_part = html.split('id="value-breakdown"')
    pattern = r'code-pill">(9\d+)</span>'
    assert re.findall(pattern, main_part) == ["9111", "9222"]  # главная таблица как была
    assert re.findall(pattern, modal_part) == ["9222", "9111"]  # разбор: дорогие сначала
    # Форма сортировки сохраняет модалку открытой и помнит выбор.
    assert 'action="#value-breakdown"' in modal_part
    assert "Сортировка" in modal_part
    assert "По умолчанию строки отсортированы по сумме" in modal_part
    html_asc = client.get(url + "?value_sort=sum_asc").content.decode()
    _, modal_asc = html_asc.split('id="value-breakdown"')
    assert re.findall(pattern, modal_asc) == ["9111", "9222"]
    assert re.search(r'<option value="sum_asc"\s+selected>', modal_asc)


# --- Черновик подхватывает исправленные цены BRP (hotfix 32.3) ----------------------


def test_draft_refreshes_corrected_brp_price(client, make_user, refs, location, admin):
    """Реимпорт прайса починил нулевую цену: черновик показывает её БЕЗ пересканирования."""
    brp = BrpCatalogPart.objects.create(
        material_no="417224916", part_desc="ROLLER ZERO", retail_price_usd=Decimal("0"),
    )
    session = start_session(location=location, by=admin)
    line = record_scan(session, "417224916", by=admin)
    set_line_quantity(line, 13)
    assert line.final_customer_price_rub == Decimal("0")  # старый нулевой снимок
    # Реимпорт BRP исправил запись каталога.
    brp.part_desc = "ROLLER PULLEY EXT"
    brp.retail_price_usd = Decimal("35.99")
    brp.save()
    before = _stock_snapshot()
    assert refresh_draft_prices(session) == 1
    line.refresh_from_db()
    assert line.final_customer_price_rub == Decimal("5291")
    assert session.counters()["total_value"] == Decimal("68783")  # 13 * 5291
    assert _stock_snapshot() == before  # освежение цен склад не трогает
    # Страница сессии показывает исправленную цену (refresh зовётся во view).
    client.login(username="admin", password=PASSWORD)
    html = client.get(reverse("counting_detail", args=[session.pk])).content.decode()
    assert "5 291" in html
    assert "68 783" in html


def test_posted_session_prices_not_refreshed(refs, location, admin):
    """Проведённые сессии — история: смена цен каталога их не переписывает."""
    session = start_session(location=location, by=admin)
    line = record_scan(session, "219800345", by=admin)  # снимок 14699
    post_session(session, by=admin)
    refs["brp_main"].retail_price_usd = Decimal("199.99")
    refs["brp_main"].save()
    before = _stock_snapshot()
    assert refresh_draft_prices(session) == 0
    line.refresh_from_db()
    assert line.final_customer_price_rub == Decimal("14699")  # снимок цел
    assert _stock_snapshot() == before


# --- Документ первичного ввода: цены из пересчёта (hotfix 33.1) ----------------------


def _session_with_priced_lines(location, admin):
    """Сессия: 417224916 x 3 по 5291 ₽ и 250000059 x 1 по 616 ₽ (итог 16489)."""
    _make_old_replaced_417()
    _make_exact_417()
    _make_zero_screw_059()
    _make_priced_screw_418()
    session = start_session(location=location, by=admin)
    line_a = record_scan(session, "417224916", by=admin)
    set_line_quantity(line_a, 3)
    record_scan(session, "250000059", by=admin)
    return session


def test_convert_uses_per_line_customer_prices(refs, location, admin):
    session = _session_with_priced_lines(location, admin)
    assert session.counters()["total_value"] == Decimal("16489")  # 3*5291 + 616
    receipt = convert_to_receipt(session, by=admin)  # глобальный unit_cost = 0
    prices = {
        line.part_type.brp_link.brp_part.material_no: (line.unit_cost_rub, line.quantity)
        for line in receipt.lines.select_related("part_type__brp_link__brp_part")
    }
    # Глобальный ноль из формы НЕ затирает цены строк пересчёта.
    assert prices["417224916"] == (Decimal("5291.00"), Decimal("3"))
    assert prices["250000059"] == (Decimal("616.00"), Decimal("1"))
    # Итог документа равен «Стоимости ячейки».
    assert receipt_totals(receipt)["cost"] == Decimal("16489.00")


def test_convert_zero_price_line_keeps_zero(refs, location, admin):
    _make_zero_screw_059()  # связанных цен нет вообще
    session = start_session(location=location, by=admin)
    record_scan(session, "250000059", by=admin)
    receipt = convert_to_receipt(session, by=admin)
    assert receipt.lines.get().unit_cost_rub == Decimal("0.00")  # цену не выдумали


def test_convert_fallback_only_for_unpriced_lines(refs, location, admin):
    _make_zero_screw_059()
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)  # цена 14699
    record_scan(session, "250000059", by=admin)  # цены нет
    receipt = convert_to_receipt(session, by=admin, unit_cost=Decimal("50"))
    prices = sorted(line.unit_cost_rub for line in receipt.lines.all())
    # Запасная цена применяется ТОЛЬКО к строке без цены.
    assert prices == [Decimal("50.00"), Decimal("14699.00")]


def test_repair_command_dry_run_and_commit(refs, location, admin):
    """Продакшен-кейс POS-000003: документ создан с нулями, чинится командой."""
    session = _session_with_priced_lines(location, admin)
    receipt = convert_to_receipt(session, by=admin)
    receipt.lines.update(unit_cost_rub=Decimal("0"))  # симуляция бага до 33.1
    post_session(session, by=admin)
    before_stock = _stock_snapshot()
    before_qty = sorted(receipt.lines.values_list("quantity", flat=True))
    # Dry-run: полный отчёт, но ничего не записано.
    out = io.StringIO()
    call_command("repair_counting_receipt_prices", session_id=session.pk, stdout=out)
    text = out.getvalue()
    assert "DRY-RUN" in text
    assert receipt.number in text
    assert "5291" in text and "616" in text  # новые цены
    assert "0.00" in text  # старые цены
    assert "16489" in text  # новый итог документа
    assert set(receipt.lines.values_list("unit_cost_rub", flat=True)) == {Decimal("0.00")}
    # Commit (по id документа): цены обновлены, склад и количества целы.
    call_command(
        "repair_counting_receipt_prices",
        receipt_id=receipt.pk, commit=True, stdout=io.StringIO(),
    )
    assert receipt_totals(receipt)["cost"] == Decimal("16489.00")
    assert sorted(receipt.lines.values_list("quantity", flat=True)) == before_qty
    assert _stock_snapshot() == before_stock  # остатки/движения/карточки не тронуты
    # Повторный запуск: менять нечего.
    out = io.StringIO()
    call_command("repair_counting_receipt_prices", session_id=session.pk, stdout=out)
    assert "менять нечего" in out.getvalue()


def test_repair_refuses_non_counting_receipt(refs, admin):
    receipt = create_receipt(supplier=Supplier.objects.create(name="Обычный поставщик"), by=admin)
    with pytest.raises(CommandError, match="не связан с пересчётом"):
        call_command("repair_counting_receipt_prices", receipt_id=receipt.pk)


def test_convert_page_explains_prices_from_counting(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    html = client.get(reverse("counting_convert", args=[session.pk])).content.decode()
    assert "Документ первичного ввода" in html
    assert "Цены будут взяты из пересчёта ячейки" in html
    assert "Итоговая стоимость документа" in html
    assert "14 699" in html
    assert "Запасная цена только для строк без цены" in html
    assert "по желанию" not in html  # старая формулировка убрана


def test_receipt_detail_labels_for_counting_receipt(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    receipt = convert_to_receipt(session, by=admin)
    html = client.get(reverse("receipt_detail", args=[receipt.pk])).content.decode()
    assert "Оценка за ед. (₽)" in html
    assert "Сумма оценки" in html
    assert "Документ первичного ввода из пересчёта ячейки" in html
    assert "Сумма себестоимости" not in html
    assert "14 699" in html  # итог совпадает со стоимостью пересчёта


def test_receipt_detail_labels_for_supplier_receipt(client, make_user, refs, admin):
    client.login(username="admin", password=PASSWORD)
    receipt = create_receipt(supplier=Supplier.objects.create(name="Поставщик"), by=admin)
    html = client.get(reverse("receipt_detail", args=[receipt.pk])).content.decode()
    assert "Сумма себестоимости" in html  # обычные поступления не изменились
    assert "Оценка за ед." not in html


# --- Layer 34: пересчёт попадает в «Инвентаризацию», а не в «Поступления» ------------


def test_post_assigns_inventory_number(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    session.refresh_from_db()
    assert session.inventory_number.startswith("IC-")  # документ инвентаризации


def test_counting_post_redirects_to_stocktaking_document(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    convert_to_receipt(session, by=admin)
    resp = client.post(reverse("counting_post", args=[session.pk]), follow=True)
    session.refresh_from_db()
    assert resp.redirect_chain[-1][0] == reverse("initial_inventory_detail", args=[session.pk])
    text = resp.content.decode()
    assert "Пересчёт завершён" in text
    assert f"Создан документ инвентаризации {session.inventory_number}" in text
    # На страницах пересчёта нет ссылки на технический документ поступления.
    detail = client.get(reverse("counting_detail", args=[session.pk])).content.decode()
    assert f"/receipts/{session.converted_receipt_id}/" not in detail
    assert session.converted_receipt.number not in detail
    assert session.inventory_number in detail


def test_receipts_list_hides_counting_documents(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    session.refresh_from_db()
    supplier_receipt = create_receipt(
        supplier=Supplier.objects.create(name="Реальный поставщик"), by=admin
    )
    html = client.get(reverse("receipt_list")).content.decode()
    assert session.converted_receipt.number not in html  # технический документ скрыт
    assert supplier_receipt.number in html  # реальная поставка на месте
    # Сам документ физически цел (лоты/движения ссылаются на него).
    assert session.converted_receipt.status == "posted"


def test_stocktaking_shows_initial_inventory_with_lines(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    line = record_scan(session, "219800345", by=admin)  # 14699
    set_line_quantity(line, 13)  # ручная правка количества
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    session.refresh_from_db()
    # Список «Инвентаризации»: блок первичного ввода.
    html = client.get(reverse("inventory_count_list")).content.decode()
    assert "Первичный ввод ячеек" in html
    assert session.inventory_number in html
    assert "B-S01-L02-D03-C08" in html
    assert "Итоговое количество" in html
    # Документ: строки заполнены автоматически, ручное количество на месте.
    html = client.get(reverse("initial_inventory_detail", args=[session.pk])).content.decode()
    assert f"Инвентаризация {session.inventory_number}" in html
    assert "219800345" in html and "BELT DRIVE" in html
    assert "Оценка за ед. (₽)" in html and "Сумма оценки" in html
    assert "13" in html  # ручная правка попала в документ
    assert "191 087" in html  # итог = сумме количество x оценка
    assert "Технический документ проведения" in html
    assert "—" not in html


def test_migrate_command_is_idempotent(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    # Симуляция прода до Layer 34: у проведённой сессии нет IC-номера.
    InventoryCountingSession.objects.filter(pk=session.pk).update(inventory_number="")
    before = _stock_snapshot()
    out = io.StringIO()
    call_command("migrate_counting_receipts_to_stocktaking", stdout=out)
    text = out.getvalue()
    assert "DRY-RUN" in text and "БУДЕТ ПРИСВОЕН" in text
    session.refresh_from_db()
    assert session.inventory_number == ""  # dry-run ничего не записал
    out = io.StringIO()
    call_command("migrate_counting_receipts_to_stocktaking", commit=True, stdout=out)
    session.refresh_from_db()
    assert session.inventory_number.startswith("IC-")
    assert session.converted_receipt.number in out.getvalue()  # отчёт о скрытых
    assert _stock_snapshot() == before  # склад не тронут
    # Повторный запуск: дублей и изменений нет.
    number = session.inventory_number
    out = io.StringIO()
    call_command("migrate_counting_receipts_to_stocktaking", commit=True, stdout=out)
    session.refresh_from_db()
    assert session.inventory_number == number
    assert "Пропущено (номер уже есть): 1" in out.getvalue()


def test_delete_draft_receipts_command(refs, location, admin):
    draft = create_receipt(supplier=Supplier.objects.create(name="Демо"), by=admin)
    add_line(draft, part_type=refs["wh"], quantity=Decimal("1"),
             unit_cost_rub=Decimal("10"), location=location)
    # Dry-run не удаляет.
    out = io.StringIO()
    call_command("delete_draft_receipts", receipt_id=draft.pk, stdout=out)
    assert "DRY-RUN" in out.getvalue()
    draft.refresh_from_db()
    # Commit удаляет черновик (склада у черновика нет).
    before = _stock_snapshot()
    call_command("delete_draft_receipts", receipt_id=draft.pk, commit=True,
                 stdout=io.StringIO())
    from apps.receipts.models import Receipt

    assert not Receipt.objects.filter(pk=draft.pk).exists()
    assert _stock_snapshot() == before
    # Проведённый документ удалить нельзя.
    posted = create_receipt(supplier=Supplier.objects.create(name="Пост"), by=admin)
    add_line(posted, part_type=refs["wh"], quantity=Decimal("1"),
             unit_cost_rub=Decimal("10"), location=location)
    post_receipt(posted, by=admin)
    with pytest.raises(CommandError, match="не черновик"):
        call_command("delete_draft_receipts", receipt_id=posted.pk)
    # Документ пересчёта удалить нельзя (даже черновик).
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    counting_receipt = convert_to_receipt(session, by=admin)
    with pytest.raises(CommandError, match="связан с пересчётом"):
        call_command("delete_draft_receipts", receipt_id=counting_receipt.pk)


def test_counting_detail_renamed_counters_and_hint(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    html = client.get(reverse("counting_detail", args=[session.pk])).content.decode()
    assert "Событий сканирования" in html
    assert "Итоговое количество" in html
    assert "Всего сканов" not in html
    assert "Всего деталей в ячейке" not in html
    assert "считается по столбцу" in html
    assert "из-за ручных правок, отмен и удалений" in html


# --- Удаление черновика (hotfix 32.2) -----------------------------------------------


def test_empty_draft_can_be_deleted(refs, location, admin):
    session = start_session(location=location, by=admin)
    assert can_delete_session(session) is True
    address = delete_session(session)
    assert address == "B-S01-L02-D03-C08"
    assert InventoryCountingSession.objects.count() == 0


def test_draft_with_scans_deleted_with_events_and_lines(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    record_scan(session, "219800345", by=admin)
    record_scan(session, "NO-SUCH-999", by=admin)
    before = _stock_snapshot()
    delete_session(session)
    assert InventoryCountingSession.objects.count() == 0
    assert InventoryScanEvent.objects.count() == 0  # сырые сканы удалены
    assert not InventoryCountingSession.objects.exists()
    from apps.counting.models import InventoryCountingLine

    assert InventoryCountingLine.objects.count() == 0  # строки удалены
    # Место хранения остаётся: адрес переиспользуется.
    assert StorageLocation.objects.filter(code="B-S01-L02-D03-C08").exists()
    assert _stock_snapshot() == before  # склад не изменился


def test_converted_session_cannot_be_deleted(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    convert_to_receipt(session, by=admin)
    assert can_delete_session(session) is False
    with pytest.raises(CountingError):
        delete_session(session)
    assert InventoryCountingSession.objects.count() == 1


def test_posted_session_cannot_be_deleted(refs, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    before = _stock_snapshot()
    with pytest.raises(CountingError):
        delete_session(session)
    assert InventoryCountingSession.objects.count() == 1
    assert _stock_snapshot() == before  # остатки и документы целы


def test_cancelled_session_cannot_be_deleted(refs, location, admin):
    session = start_session(location=location, by=admin)
    cancel_session(session)
    assert can_delete_session(session) is False
    with pytest.raises(CountingError):
        delete_session(session)


def test_list_shows_delete_only_for_drafts(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    draft = start_session(location=location, by=admin)
    posted = start_session(location=location, by=admin)
    record_scan(posted, "700700", by=admin)
    post_session(posted, by=admin)
    html = client.get(reverse("counting_list")).content.decode()
    assert reverse("counting_delete", args=[draft.pk]) in html
    assert reverse("counting_delete", args=[posted.pk]) not in html
    assert "Открыть" in html


def test_delete_confirmation_page(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    session.comment = "Тестовая ячейка"
    session.save(update_fields=["comment"])
    record_scan(session, "219800345", by=admin)
    record_scan(session, "219800345", by=admin)
    html = client.get(reverse("counting_delete", args=[session.pk])).content.decode()
    assert "Удалить черновик инвентаризации ячейки" in html
    assert "B-S01-L02-D03-C08" in html
    assert "Черновик" in html  # статус
    assert "Тестовая ячейка" in html  # описание
    assert "Остатки склада не изменятся" in html
    assert "—" not in html


def test_delete_confirmation_blocked_for_posted(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    html = client.get(reverse("counting_delete", args=[session.pk])).content.decode()
    assert "удалить нельзя" in html
    assert "Вернуться к списку" in html
    # Кнопки удаления нет.
    assert '<button type="submit" class="btn btn--danger">Удалить</button>' not in html


def test_post_delete_redirects_with_message(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "219800345", by=admin)
    resp = client.post(reverse("counting_delete", args=[session.pk]), follow=True)
    assert resp.redirect_chain[-1][0] == reverse("counting_list")
    text = resp.content.decode()
    assert "Черновик инвентаризации ячейки B-S01-L02-D03-C08 удалён." in text
    assert InventoryCountingSession.objects.count() == 0


def test_direct_post_delete_blocked_for_non_draft(client, make_user, refs, location, admin):
    client.login(username="admin", password=PASSWORD)
    session = start_session(location=location, by=admin)
    record_scan(session, "700700", by=admin)
    post_session(session, by=admin)
    before = _stock_snapshot()
    resp = client.post(reverse("counting_delete", args=[session.pk]), follow=True)
    text = resp.content.decode()
    assert "удалить нельзя" in text
    assert InventoryCountingSession.objects.count() == 1
    assert _stock_snapshot() == before


def test_delete_requires_permission(client, make_user, refs, location, admin):
    session = start_session(location=location, by=admin)
    # Аноним: редирект на логин, сессия цела.
    resp = client.post(reverse("counting_delete", args=[session.pk]))
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]
    # Продавец без can_manage_inventory: 403.
    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.post(reverse("counting_delete", args=[session.pk])).status_code == 403
    assert InventoryCountingSession.objects.count() == 1


def test_pages_have_no_em_dash(client, make_user, refs, location):
    _login(client, make_user, superuser=True, name="boss")
    session = InventoryCountingSession.objects.create(
        storage_location=location, full_address=location.code, title="t",
    )
    record_scan(session, "219800345")
    for url in (
        reverse("counting_list"),
        reverse("counting_new"),
        reverse("counting_detail", args=[session.pk]),
        reverse("counting_convert", args=[session.pk]),
    ):
        assert "—" not in client.get(url).content.decode()
