import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from django.core.management import CommandError, call_command
from django.db import close_old_connections, connection
from django.urls import reverse
from django.utils import timezone

from apps.core.scanner import resolve_scan
from apps.core.views import _resolve_move_destination
from apps.inventory.models import (
    PartPreferredLocation,
    StockBalance,
    StockLocationLock,
    StockLot,
    StockMovement,
)
from apps.stocktaking.section_recount import (
    cancel_section_recount,
    create_section_recount,
    start_section_recount,
)
from apps.warehouse.address_migration import (
    StorageAddressMigrationError,
    apply_storage_address_v2_plan,
    build_storage_address_v2_plan,
)
from apps.warehouse.addresses import (
    AddressError,
    compose_address,
    create_location,
    get_or_create_location,
    parse_address,
    parse_legacy_address,
)
from apps.warehouse.drawer_rename import (
    build_drawer_rename_plan,
    rename_storage_drawer,
)
from apps.warehouse.models import (
    StorageLocation,
    StorageLocationAlias,
    StorageLocationRenameHistory,
)
from apps.warehouse.services import (
    StorageLocationCreateError,
    StorageLocationRenameError,
    attach_movement_location_history,
    rename_storage_location,
)


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(
        username="address-v2-admin",
        password="parol-12345",
    )


def _legacy_group(*, rack=1, level=2, drawer=3, cells=(1, 2), with_drawer=True):
    prefix = f"S{rack:02d}-L{level:02d}-D{drawer:02d}"
    result = []
    if with_drawer:
        result.append(
            StorageLocation.objects.create(
                name=prefix,
                code=prefix,
                level=StorageLocation.Level.SECTION,
                storage_allowed=False,
            )
        )
    result.extend(
        StorageLocation.objects.create(
            name=f"C{cell:02d}",
            code=f"{prefix}-C{cell:02d}",
            level=StorageLocation.Level.CELL,
        )
        for cell in cells
    )
    return result


def _v2_drawer(*, rack=3, drawer=2, cells=(1, 2)):
    locations = [
        create_location(f"S{rack:02d}-D{drawer:02d}-C{cell:02d}")
        for cell in cells
    ]
    return locations[0].parent, locations


def test_v2_parser_creation_and_explicit_legacy_compatibility(db):
    assert compose_address(3, drawer_no=2, cell_no=5) == "S03-D02-C05"
    parsed = parse_address("s03-d02-c05")
    assert (parsed.rack, parsed.drawer, parsed.cell, parsed.code) == (
        3,
        2,
        5,
        "S03-D02-C05",
    )
    cell = create_location(parsed.code, name="Пятая ячейка")
    assert cell.level == StorageLocation.Level.CELL
    assert cell.parent.code == "S03-D02"
    assert cell.parent.level == StorageLocation.Level.DRAWER
    assert cell.parent.parent.code == "S03"
    assert cell.parent.parent.level == StorageLocation.Level.RACK
    assert cell.barcode == "LOC:S03-D02-C05"

    legacy = parse_legacy_address("A-S01-L02-K03-C04")
    assert legacy.drawer_code == "A-S01-L02-K03"
    with pytest.raises(AddressError, match="только в формате S-D-C"):
        get_or_create_location(legacy.raw_code)
    legacy_location = get_or_create_location(legacy.raw_code, allow_legacy=True)
    assert get_or_create_location(legacy.raw_code).pk == legacy_location.pk

    StorageLocation.objects.create(
        name="Custom barcode",
        code="CUSTOM-CODE",
        barcode="S04-D01-C01",
    )
    with pytest.raises(StorageLocationCreateError, match="таким кодом"):
        create_location("S04-D01-C01")


def test_drawer_zero_is_a_canonical_v2_identity_not_a_missing_value(db):
    assert compose_address(3, drawer_no=0, cell_no=5) == "S03-D00-C05"
    parsed = parse_address("s03-d00-c05")
    assert (parsed.rack, parsed.drawer, parsed.cell, parsed.code) == (3, 0, 5, "S03-D00-C05")
    cell = create_location(parsed.code)
    assert cell.parent.code == "S03-D00"
    assert cell.parent.sort_order == 0
    assert get_or_create_location("S03-D00-C05").pk == cell.pk
    with pytest.raises(AddressError, match="отрицательным"):
        compose_address(3, drawer_no=-1)
    with pytest.raises(AddressError):
        parse_address("S03-D-1-C05")
    # Legacy D00 remains invalid and is never reinterpreted as the new V2 D00.
    with pytest.raises(AddressError):
        parse_legacy_address("S03-L01-D00-C05")


def test_alias_resolves_old_code_and_barcode_to_same_identity(db, admin):
    cell = create_location("S03-D02-C05")
    old_id = cell.pk
    old_code = cell.code
    old_barcode = cell.barcode
    rename_storage_location(
        cell,
        new_code="S03-D02-C06",
        expected_code=old_code,
        by=admin,
    )

    for raw in (old_code, old_barcode):
        result = resolve_scan(raw)
        assert result.status == "found"
        assert result.id == old_id
        assert result.is_alias is True
        assert "Текущий адрес: S03-D02-C06" in result.message
    assert get_or_create_location(old_code).pk == old_id
    destination, error = _resolve_move_destination(old_code)
    assert error == ""
    assert destination.pk == old_id
    assert StorageLocation.objects.count() == 3


def test_destination_search_finds_v2_fragments_and_historical_alias(client, admin):
    cell = create_location("S03-D02-C05")
    rename_storage_location(
        cell,
        new_code="S03-D02-C06",
        expected_code=cell.code,
        by=admin,
    )
    client.force_login(admin)
    url = reverse("scanner_move_locations")
    for query in ("S03-D02-C06", "D02-C06", "C06", "S03-D02-C05"):
        response = client.get(url, {"q": query})
        assert response.status_code == 200
        assert response.json()["results"][0]["id"] == cell.pk
        assert response.json()["results"][0]["code"] == "S03-D02-C06"


def test_address_migration_command_defaults_to_read_only_dry_run(db, tmp_path):
    _legacy_group()
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(
        json.dumps({"S01-L02-D03": "S01-D04"}),
        encoding="utf-8",
    )
    output = io.StringIO()
    call_command("migrate_storage_addresses_v2", str(mapping_file), stdout=output)
    assert "DRY-RUN: база данных не изменена" in output.getvalue()
    assert StorageLocation.objects.filter(code__contains="-L02-").count() == 3
    assert not StorageLocationAlias.objects.exists()
    with pytest.raises(CommandError, match="confirm"):
        call_command(
            "migrate_storage_addresses_v2",
            str(mapping_file),
            apply=True,
            expected_fingerprint="not-used-without-confirm",
        )
    assert StorageLocation.objects.filter(code__contains="-L02-").count() == 3


def test_address_migration_dry_run_apply_preserves_ids_and_is_idempotent(db, admin):
    locations = _legacy_group()
    ids = {item.code: item.pk for item in locations}
    mapping = {"S01-L02-D03": "S01-D04"}
    before = {
        "movements": StockMovement.objects.count(),
        "lots": StockLot.objects.count(),
        "balances": StockBalance.objects.count(),
        "preferred": PartPreferredLocation.objects.count(),
    }

    plan = build_storage_address_v2_plan(mapping)
    assert plan.can_apply
    assert [(item.old_code, item.new_code) for item in plan.changes] == [
        ("S01-L02-D03", "S01-D04"),
        ("S01-L02-D03-C01", "S01-D04-C01"),
        ("S01-L02-D03-C02", "S01-D04-C02"),
    ]
    assert StorageLocation.objects.filter(code__contains="-L02-").count() == 3

    result = apply_storage_address_v2_plan(
        mapping,
        expected_fingerprint=plan.fingerprint,
        by=admin,
    )
    assert result["updated_locations"] == 3
    for old_code, old_id in ids.items():
        alias = StorageLocationAlias.objects.get(code=old_code)
        assert alias.location_id == old_id
    assert StorageLocation.objects.get(code="S01-D04").pk == ids["S01-L02-D03"]
    assert StorageLocation.objects.get(code="S01-D04-C01").pk == ids["S01-L02-D03-C01"]
    assert StorageLocation.objects.get(code="S01-D04-C02").pk == ids["S01-L02-D03-C02"]
    assert StorageLocation.objects.get(code="S01-D04-C01").parent.code == "S01-D04"
    assert StorageLocationRenameHistory.objects.filter(
        reason=StorageLocationRenameHistory.Reason.ADDRESS_V2
    ).count() == 3
    assert before == {
        "movements": StockMovement.objects.count(),
        "lots": StockLot.objects.count(),
        "balances": StockBalance.objects.count(),
        "preferred": PartPreferredLocation.objects.count(),
    }

    repeated = build_storage_address_v2_plan(mapping)
    assert repeated.can_apply
    assert not repeated.changes
    assert repeated.already_applied == ["S01-L02-D03 -> S01-D04"]


def test_address_migration_creates_missing_parents_for_flat_cells(db, admin):
    cells = _legacy_group(with_drawer=False)
    ids = [item.pk for item in cells]
    mapping = {"S01-L02-D03": "S01-D04"}
    plan = build_storage_address_v2_plan(mapping)
    assert plan.create_racks == ["S01"]
    assert plan.create_drawers == ["S01-D04"]
    result = apply_storage_address_v2_plan(
        mapping,
        expected_fingerprint=plan.fingerprint,
        by=admin,
    )
    assert result["created_parents"] == ["S01", "S01-D04"]
    assert list(
        StorageLocation.objects.filter(pk__in=ids).order_by("pk").values_list("code", flat=True)
    ) == ["S01-D04-C01", "S01-D04-C02"]


def test_address_migration_blocks_collision_unmapped_lock_and_stale_state(db, admin):
    locations = _legacy_group()
    mapping = {"S01-L02-D03": "S01-D04"}
    target = create_location("S01-D04-C01")
    collision = build_storage_address_v2_plan(mapping)
    assert not collision.can_apply
    assert any(target.code in message for message in collision.conflicts)
    target.delete()
    target.parent.delete()
    target.parent = None

    unmapped = build_storage_address_v2_plan({})
    assert not unmapped.can_apply
    assert "S01-L02-D03-C01" in unmapped.unmapped

    lock = StockLocationLock.objects.create(
        location=locations[-1],
        section_code="TEST",
        document_id=99,
    )
    locked = build_storage_address_v2_plan(mapping)
    assert not locked.can_apply
    lock.delete()

    safe = build_storage_address_v2_plan(mapping)
    locations[-1].name = "Изменено после dry-run"
    locations[-1].save(update_fields=["name", "updated_at"])
    with pytest.raises(StorageAddressMigrationError, match="Fingerprint"):
        apply_storage_address_v2_plan(
            mapping,
            expected_fingerprint=safe.fingerprint,
            by=admin,
        )
    assert StorageLocation.objects.filter(code__contains="-L02-").count() == 3


def test_address_migration_fault_rolls_back_every_location_and_alias(db, admin):
    locations = _legacy_group()
    before = list(StorageLocation.objects.values_list("pk", "code", "parent_id", "level"))
    mapping = {"S01-L02-D03": "S01-D04"}
    plan = build_storage_address_v2_plan(mapping)
    with pytest.raises(StorageAddressMigrationError, match="Injected"):
        apply_storage_address_v2_plan(
            mapping,
            expected_fingerprint=plan.fingerprint,
            by=admin,
            fault_after=1,
        )
    assert list(StorageLocation.objects.values_list("pk", "code", "parent_id", "level")) == before
    assert not StorageLocationAlias.objects.exists()
    assert not StorageLocationRenameHistory.objects.exists()
    assert {item.pk for item in locations} == set(
        StorageLocation.objects.values_list("pk", flat=True)
    )


def test_v2_section_recount_uses_actual_drawer_children(db, admin):
    drawer, cells = _v2_drawer(cells=(1, 3, 8))
    doc = create_section_recount(section_code=drawer.code, by=admin)
    started = start_section_recount(doc)
    assert list(started.cells.order_by("sequence").values_list("location_id", flat=True)) == [
        item.pk for item in cells
    ]
    assert StockLocationLock.objects.filter(document_id=doc.pk).count() == 3
    cancel_section_recount(doc)


def test_drawer_rename_preserves_ids_adds_grouped_history_and_aliases(db, admin):
    drawer, cells = _v2_drawer()
    ids = {item.code: item.pk for item in [drawer, *cells]}
    before = {
        "movements": StockMovement.objects.count(),
        "lots": StockLot.objects.count(),
        "balances": StockBalance.objects.count(),
    }
    preview = build_drawer_rename_plan(drawer, 5)
    assert preview.can_apply
    assert [(item.old_code, item.new_code) for item in preview.changes] == [
        ("S03-D02", "S03-D05"),
        ("S03-D02-C01", "S03-D05-C01"),
        ("S03-D02-C02", "S03-D05-C02"),
    ]
    renamed = rename_storage_drawer(
        drawer,
        new_number=5,
        expected_code=drawer.code,
        expected_fingerprint=preview.fingerprint,
        by=admin,
    )
    assert renamed.pk == ids["S03-D02"]
    assert StorageLocation.objects.get(code="S03-D05-C01").pk == ids["S03-D02-C01"]
    assert StorageLocation.objects.get(code="S03-D05-C02").pk == ids["S03-D02-C02"]
    histories = list(
        StorageLocationRenameHistory.objects.filter(
            reason=StorageLocationRenameHistory.Reason.DRAWER
        )
    )
    assert len(histories) == 3
    assert len({item.operation_key for item in histories}) == 1
    assert StorageLocationAlias.objects.filter(kind=StorageLocationAlias.Kind.DRAWER).count() == 3
    assert resolve_scan("LOC:S03-D02-C01").id == ids["S03-D02-C01"]
    with pytest.raises(StorageLocationCreateError, match="старым адресом"):
        create_location("S03-D02-C09")
    assert before == {
        "movements": StockMovement.objects.count(),
        "lots": StockLot.objects.count(),
        "balances": StockBalance.objects.count(),
    }


def test_drawer_can_be_renamed_to_zero_without_mutating_stock(db, admin):
    drawer, cells = _v2_drawer()
    before = {
        "movements": StockMovement.objects.count(),
        "lots": StockLot.objects.count(),
        "balances": StockBalance.objects.count(),
    }
    preview = build_drawer_rename_plan(drawer, 0)
    assert preview.new_code == "S03-D00"
    renamed = rename_storage_drawer(
        drawer,
        new_number=0,
        expected_code=drawer.code,
        expected_fingerprint=preview.fingerprint,
        by=admin,
    )
    assert renamed.code == "S03-D00"
    assert StorageLocation.objects.get(pk=cells[0].pk).code == "S03-D00-C01"
    assert before == {
        "movements": StockMovement.objects.count(),
        "lots": StockLot.objects.count(),
        "balances": StockBalance.objects.count(),
    }


def test_drawer_rename_blocks_collision_lock_stale_and_same_number(db, admin):
    drawer, cells = _v2_drawer()
    create_location("S03-D05-C01")
    collision = build_drawer_rename_plan(drawer, 5)
    assert not collision.can_apply
    with pytest.raises(StorageLocationRenameError, match="совпадает"):
        build_drawer_rename_plan(drawer, 2)

    target_drawer = StorageLocation.objects.get(code="S03-D05")
    target_drawer.children.all().delete()
    target_drawer.delete()
    lock = StockLocationLock.objects.create(
        location=cells[0],
        section_code="TEST",
        document_id=101,
    )
    locked = build_drawer_rename_plan(drawer, 5)
    assert not locked.can_apply
    lock.delete()

    safe = build_drawer_rename_plan(drawer, 5)
    cells[0].name = "Изменено"
    cells[0].save(update_fields=["name", "updated_at"])
    with pytest.raises(StorageLocationRenameError, match="устарел"):
        rename_storage_drawer(
            drawer,
            new_number=5,
            expected_code=drawer.code,
            expected_fingerprint=safe.fingerprint,
            by=admin,
        )


def test_drawer_rename_fault_rolls_back_and_ui_requires_permission(
    db, admin, client, django_user_model
):
    drawer, _cells = _v2_drawer()
    before = list(StorageLocation.objects.values_list("pk", "code", "barcode"))
    preview = build_drawer_rename_plan(drawer, 5)
    with pytest.raises(StorageLocationRenameError, match="Injected"):
        rename_storage_drawer(
            drawer,
            new_number=5,
            expected_code=drawer.code,
            expected_fingerprint=preview.fingerprint,
            by=admin,
            fault_after=2,
        )
    assert list(StorageLocation.objects.values_list("pk", "code", "barcode")) == before
    assert not StorageLocationRenameHistory.objects.exists()
    assert not StorageLocationAlias.objects.exists()

    user = django_user_model.objects.create_user("no-warehouse", password="parol-12345")
    client.force_login(user)
    url = reverse("location_drawer_rename", args=[drawer.pk])
    assert client.get(url).status_code == 403
    client.force_login(admin)
    page = client.get(url)
    assert page.status_code == 200
    response = client.post(
        url,
        {
            "expected_code": drawer.code,
            "new_number": "5",
            "action": "preview",
        },
    )
    assert response.status_code == 200
    assert response.context["preview"].new_code == "S03-D05"
    assert "S03-D02-C01" in response.content.decode()


def test_drawer_cannot_bypass_atomic_rename_through_cell_endpoint(db, admin, client):
    drawer, cells = _v2_drawer()
    client.force_login(admin)

    legacy_url = reverse("location_rename", args=[drawer.pk])
    assert client.get(legacy_url).status_code == 404
    assert client.post(
        legacy_url,
        {"expected_code": drawer.code, "new_code": "S03-D05"},
    ).status_code == 404
    edit_html = client.get(reverse("location_edit", args=[drawer.pk])).content.decode()
    assert reverse("location_drawer_rename", args=[drawer.pk]) in edit_html
    assert reverse("location_rename", args=[drawer.pk]) not in edit_html
    with pytest.raises(StorageLocationRenameError, match="только для ячейки"):
        rename_storage_location(
            drawer,
            new_code="S03-D05",
            expected_code=drawer.code,
            by=admin,
        )

    drawer.refresh_from_db()
    cells[0].refresh_from_db()
    assert drawer.code == "S03-D02"
    assert cells[0].code == "S03-D02-C01"
    assert not StorageLocationRenameHistory.objects.exists()


def test_movement_history_keeps_address_at_event_after_rename(db, admin):
    location = create_location("S03-D02-C01")
    movement = SimpleNamespace(
        from_location_id=None,
        from_location=None,
        to_location_id=location.pk,
        to_location=location,
        created_at=timezone.now(),
    )
    rename_storage_location(
        location,
        new_code="S03-D02-C02",
        expected_code=location.code,
        by=admin,
    )
    location.refresh_from_db()
    movement.to_location = location
    attach_movement_location_history([movement])
    assert movement.to_location_historical_code == "S03-D02-C01"
    assert movement.to_location.code == "S03-D02-C02"
    assert movement.to_location_was_renamed is True


def _captured_call(function, *args, **kwargs):
    close_old_connections()
    try:
        return function(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - concurrency outcome is asserted below.
        return exc
    finally:
        close_old_connections()


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PostgreSQL row-lock integration test",
)
def test_postgresql_concurrent_drawer_rename_has_one_atomic_winner(admin):
    drawer, _cells = _v2_drawer()
    preview = build_drawer_rename_plan(drawer, 5)
    kwargs = {
        "new_number": 5,
        "expected_code": drawer.code,
        "expected_fingerprint": preview.fingerprint,
        "by": admin,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _captured_call(rename_storage_drawer, drawer, **kwargs),
                range(2),
            )
        )
    assert sum(isinstance(result, StorageLocation) for result in results) == 1
    assert sum(isinstance(result, StorageLocationRenameError) for result in results) == 1
    assert StorageLocation.objects.filter(code="S03-D05").count() == 1
    assert StorageLocationRenameHistory.objects.count() == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PostgreSQL row-lock integration test",
)
def test_postgresql_concurrent_address_mapping_has_one_atomic_winner(admin):
    _legacy_group()
    mapping = {"S01-L02-D03": "S01-D04"}
    preview = build_storage_address_v2_plan(mapping)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _captured_call(
                    apply_storage_address_v2_plan,
                    mapping,
                    expected_fingerprint=preview.fingerprint,
                    by=admin,
                ),
                range(2),
            )
        )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, StorageAddressMigrationError) for result in results) == 1
    assert StorageLocation.objects.filter(code="S01-D04-C01").count() == 1
    assert StorageLocationRenameHistory.objects.count() == 3
