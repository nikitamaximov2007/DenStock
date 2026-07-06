"""Layer 32 — быстрая инвентаризация ячейки сканером.

Гарантии: скан и черновик сессии склад НЕ меняют; одинаковые номера
группируются; сырые скан-события хранятся; отмена уменьшает количество;
склад имеет приоритет над BRP; позиции BRP при проведении автоматически
становятся складскими карточками (со снимком цены без округления); остаток
пишется по адресу только при проведении; повторное проведение сессии
запрещено (защита от удвоения).
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.counting.models import InventoryCountingSession, InventoryScanEvent
from apps.counting.services import (
    CountingError,
    cancel_session,
    convert_to_receipt,
    post_session,
    record_scan,
    start_session,
    undo_last_scan,
)
from apps.inventory.models import StockBalance, StockMovement
from apps.warehouse.addresses import compose_address, get_or_create_location
from apps.warehouse.models import StorageLocation

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
    assert m.final_customer_price_rub == Decimal("14698.53")  # 99.99*105*1.4 без округления


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
    assert link.calculated_customer_price_rub == Decimal("14698.53")  # без округления
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


def test_scan_endpoint_enter_handling(client, make_user, refs, location):
    _login(client, make_user, superuser=True, name="boss")
    session = InventoryCountingSession.objects.create(
        storage_location=location, full_address=location.code, title="t",
    )
    resp = client.post(reverse("counting_scan", args=[session.pk]), {"code": "219800345"})
    assert resp.status_code == 302  # PRG: возврат на страницу сканера
    assert session.lines.count() == 1
    assert session.lines.get().brp_catalog_part == refs["brp_main"]


def test_new_session_creates_location(client, make_user, refs):
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(
        reverse("counting_new"),
        {
            "zone_code": "Q", "rack_number": "1", "level_number": "1",
            "place_type": "box", "place_number": "1", "cell_number": "3", "comment": "",
        },
    )
    assert resp.status_code == 302
    assert StorageLocation.objects.filter(code="Q-S01-L01-X01-C03").exists()


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
