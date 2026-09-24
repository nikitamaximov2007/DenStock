"""Existing rows already persisted with manufacturer="BRP" by the old default.

The manufacturer-defaulting fix (apps/actions/models.py) stops NEW cards from
defaulting to "BRP", but rows already frozen into a ``PartCustomsDataVersion``
(or a live ``PartCustomsInfo`` nobody has re-saved yet) keep saying "BRP"
verbatim - versions are immutable, and this repository's own rule is not to
rewrite historical documents. This file proves the two-layer answer:

1. Read time (``apps.actions.services.authoritative_manufacturer``,
   wired into ``_customs_row_from_version``/``part_export_data``): an
   unproven "BRP" is re-checked against the same evidence a real save would
   use, on every read, with zero writes. History/Excel/the customs order
   queue never trust a stale "BRP" blindly, even if nobody ever runs the
   repair command below.

2. Write time (``manage.py repair_customs_manufacturers``): an explicit,
   dry-run-by-default, evidence-gated command that corrects the LIVE
   ``PartCustomsInfo.manufacturer`` field so the next real card edit does not
   re-freeze the same stale value into a new version. Tier 1 (default
   --apply) only relabels to a DIFFERENT, currently-proven brand. Tier 2
   (--apply --clear-unproven-brp, a separate opt-in) clears a "BRP" with NO
   evidence at all to unknown - the task explicitly wants a human gate before
   that step even though nothing else could have produced it.
"""
import json
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.actions.models import PartCustomsDataVersion, PartCustomsInfo
from apps.actions.services import (
    authoritative_manufacturer,
    historical_analog_customs_rows,
    historical_customs_rows,
    is_brp_export_eligible,
)
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.services import create_manual_part
from apps.customs_orders.services import customs_sources, eligible_customs_sources
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale
from apps.warehouse.models import StorageLocation

pytestmark = pytest.mark.django_db

ApplicationArea = PartCustomsInfo.ApplicationArea


# --- Обстановка ---------------------------------------------------------------


@pytest.fixture
def env(django_user_model):
    from apps.suppliers.models import Supplier

    admin = django_user_model.objects.create_superuser(username="boss", password="parol-12345")
    supplier, _ = Supplier.objects.get_or_create(name="ООО Поставка")
    location, _ = StorageLocation.objects.get_or_create(
        code="S01-D01-C01",
        defaults={"name": "Ячейка", "storage_allowed": True, "is_active": True},
    )
    return {"admin": admin, "sup": supplier, "loc": location}


def _receive(env, part, quantity="10"):
    batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["loc"], Decimal(quantity))
    receive_stock_lot(lot, by=env["admin"])


def _stale_brp_part(env, *, name, article, manufacturer_name=""):
    """Ручная деталь, чья таможенная карточка уже сохранена с manufacturer="BRP" -
    ровно так, как это делал старый default модели (или прямая запись до
    исправления). ``manufacturer_name`` задаёт РЕАЛЬНОГО производителя детали
    (PartType.manufacturer), если он известен независимо от карточки."""
    part = create_manual_part(
        name=name, article=article, price="1000", manufacturer_name=manufacturer_name,
    )
    _receive(env, part)
    # Полная карточка, включая вес/область - деталь готова к продаже без
    # обходных путей вроде legacy_customs_completion. Ключевой факт теста:
    # manufacturer="BRP" сохранён БУКВАЛЬНО, независимо от того, что доказано
    # (или не доказано) у самой детали.
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="ДЕТАЛЬ", customs_name_ru_confirmed=True,
        customs_name_en="PART", manufacturer="BRP", country_of_origin="CANADA",
        gross_weight_kg=Decimal("0.250"), net_weight_kg=Decimal("0.200"),
        customs_unit_price_usd=Decimal("10.00"),
        application_area=ApplicationArea.SNOWMOBILE,
    )
    return part


def _sell(env, part, *, number):
    from apps.actions.services import perform_action

    return perform_action(
        part=part, location=env["loc"], action_type="sale", quantity="1",
        customer_comment="Клиент", scanned_number=number, by=env["admin"],
    )


def _row_for(rows, number):
    found = [r for r in rows if r["number"] == number]
    assert len(found) == 1, [r["number"] for r in rows]
    return found[0]


# --- 1-4: stale BRP + authoritative brand -------------------------------------


@pytest.mark.parametrize(
    "brand, eligible", [("BRONCO", False), ("SPI", False), ("PROX", True), ("MOTUL", False)],
)
def test_stale_brp_resolves_to_the_proven_brand(env, brand, eligible):
    """Карточка сохранена с "BRP", но PartType.manufacturer доказывает другое -
    на чтении побеждает доказательство, а не сохранённая строка."""
    part = _stale_brp_part(env, name=f"ДЕТАЛЬ {brand}", article=f"STALE-{brand}",
                            manufacturer_name=brand)
    info = PartCustomsInfo.objects.get(part_type=part)
    assert info.manufacturer == "BRP"  # сохранённое значение - именно "BRP"
    _sell(env, part, number=f"STALE-{brand}")

    # SPI - единственный бренд, который canonical_customs_lines сам считает
    # аналогом (см. _is_analog_part), поэтому его строка идёт в аналоговую
    # выгрузку, а не в обычную - это существующее, отдельно проверенное
    # правило, не связанное с этим фиксом.
    rows = historical_analog_customs_rows() if brand == "SPI" else historical_customs_rows()
    row = _row_for(rows, f"STALE-{brand}")
    assert row["manufacturer"] == brand  # не "BRP"
    assert is_brp_export_eligible(row["manufacturer"]) is eligible

    articles = {r["number"] for r in eligible_customs_sources()}
    assert (f"STALE-{brand}" in articles) is eligible
    # «История» показывает всё независимо от допуска.
    assert f"STALE-{brand}" in {r["number"] for r in customs_sources()}


def test_stale_brp_with_no_evidence_is_not_treated_as_proven(env):
    """Карточка сохранена с "BRP", но ничего в системе это не доказывает -
    не считается проверенным BRP ни для показа, ни для допуска к экспорту."""
    part = _stale_brp_part(env, name="ЗАГАДКА", article="STALE-UNKNOWN")
    _sell(env, part, number="STALE-UNKNOWN")

    row = _row_for(historical_customs_rows(), "STALE-UNKNOWN")
    assert row["manufacturer"] == ""  # не "BRP" - недоказано
    assert not is_brp_export_eligible(row["manufacturer"])
    assert "STALE-UNKNOWN" not in {r["number"] for r in eligible_customs_sources()}
    # Видно в истории (не потеряно), просто не допущено к отправке.
    assert "STALE-UNKNOWN" in {r["number"] for r in customs_sources()}


def test_proven_brp_stays_brp_and_eligible(env):
    """Настоящий BRP (каталожная связь) не путается со stale-строками."""
    brp = BrpCatalogPart.objects.create(
        material_no="219800345", part_desc="BELT DRIVE",
        wholesale_price_usd=Decimal("28.15"),
    )
    part = promote_to_warehouse(brp, by=env["admin"])
    _receive(env, part)
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="РЕМЕНЬ", customs_name_ru_confirmed=True,
        customs_name_en="BELT", manufacturer="BRP", country_of_origin="CANADA",
        gross_weight_kg=Decimal("0.250"), net_weight_kg=Decimal("0.200"),
        customs_unit_price_usd=Decimal("10.00"),
        application_area=ApplicationArea.SNOWMOBILE,
    )
    _sell(env, part, number="219800345")

    row = _row_for(historical_customs_rows(), "219800345")
    assert row["manufacturer"] == "BRP"
    assert is_brp_export_eligible(row["manufacturer"])
    assert "219800345" in {r["number"] for r in eligible_customs_sources()}


def test_export_is_correct_even_without_ever_running_the_repair_command(env):
    """Раздел 7 задания: экспорт безопасен ДО и БЕЗ ремонта данных."""
    part = _stale_brp_part(env, name="BRONCO ДЕТАЛЬ", article="AT-08776",
                            manufacturer_name="BRONCO")
    _sell(env, part, number="AT-08776")
    # Ремонт carточки НЕ запускался - только чтение.
    assert PartCustomsInfo.objects.get(part_type=part).manufacturer == "BRP"
    assert "AT-08776" not in {r["number"] for r in eligible_customs_sources()}
    assert _row_for(historical_customs_rows(), "AT-08776")["manufacturer"] == "BRONCO"


# --- 5, 12: UI resolver does not confidently expose known-wrong stale BRP ----


def test_authoritative_manufacturer_never_returns_an_unproven_brp(env):
    part = _stale_brp_part(env, name="ЗАГАДКА 2", article="STALE-UNKNOWN-2")
    resolved = authoritative_manufacturer(part, "BRP")
    assert resolved == ""  # никогда не "BRP" без доказательства
    assert resolved != "BRP"


def test_authoritative_manufacturer_trusts_a_non_brp_declared_value(env):
    """Не-BRP значение никогда не перепроверяется - его неоткуда взять
    случайно (форма не даёт вписать производителя руками)."""
    part = _stale_brp_part(env, name="SPI ДЕТАЛЬ", article="SPI-1", manufacturer_name="SPI")
    assert authoritative_manufacturer(part, "SPI") == "SPI"


# --- 7-10: repair command -------------------------------------------------------


def _run_repair(*args):
    out = StringIO()
    call_command("repair_customs_manufacturers", *args, stdout=out)
    return out.getvalue()


def test_manufacturer_audit_uses_bounded_queries(env):
    _stale_brp_part(env, name="BRONCO ДЕТАЛЬ", article="AUDIT-BRONCO", manufacturer_name="BRONCO")
    _stale_brp_part(env, name="ЗАГАДКА", article="AUDIT-UNKNOWN")
    output = StringIO()
    with CaptureQueriesContext(connection) as queries:
        call_command(
            "audit_customs_manufacturer_classification",
            "--json",
            "--list",
            "0",
            stdout=output,
        )

    payload = json.loads(output.getvalue())
    assert payload["parts_with_customs_info"] == 2
    assert payload["stale_brp_high_confidence"] == 1
    assert payload["stale_brp_ambiguous_needs_owner_review"] == 1
    assert len(queries.captured_queries) <= 20


def test_repair_dry_run_writes_nothing(env):
    part = _stale_brp_part(env, name="BRONCO ДЕТАЛЬ", article="AT-08776",
                            manufacturer_name="BRONCO")
    before = PartCustomsInfo.objects.get(part_type=part).manufacturer
    version_count_before = PartCustomsDataVersion.objects.filter(part_type=part).count()
    output = _run_repair()
    assert "dry_run: True" in output
    info = PartCustomsInfo.objects.get(part_type=part)
    assert info.manufacturer == before == "BRP"
    assert PartCustomsDataVersion.objects.filter(part_type=part).count() == version_count_before


def test_repair_apply_changes_only_high_confidence_rows(env):
    bronco = _stale_brp_part(env, name="BRONCO ДЕТАЛЬ", article="AT-08776",
                              manufacturer_name="BRONCO")
    ambiguous = _stale_brp_part(env, name="ЗАГАДКА", article="STALE-UNKNOWN")
    proven = _stale_brp_part(env, name="ПРОВЕРЕННЫЙ", article="RB-1", manufacturer_name="BRP")

    _run_repair("--apply")

    assert PartCustomsInfo.objects.get(part_type=bronco).manufacturer == "BRONCO"
    # Tier 2 (без доказательств) НЕ трогается без --clear-unproven-brp.
    assert PartCustomsInfo.objects.get(part_type=ambiguous).manufacturer == "BRP"
    # Доказанный BRP не трогается вовсе.
    assert PartCustomsInfo.objects.get(part_type=proven).manufacturer == "BRP"


def test_repair_apply_clear_unproven_requires_explicit_flag(env):
    ambiguous = _stale_brp_part(env, name="ЗАГАДКА", article="STALE-UNKNOWN")
    _run_repair("--apply")  # без --clear-unproven-brp
    assert PartCustomsInfo.objects.get(part_type=ambiguous).manufacturer == "BRP"
    _run_repair("--apply", "--clear-unproven-brp")
    assert PartCustomsInfo.objects.get(part_type=ambiguous).manufacturer == ""


def test_repair_is_idempotent(env):
    bronco = _stale_brp_part(env, name="BRONCO ДЕТАЛЬ", article="AT-08776",
                              manufacturer_name="BRONCO")
    _run_repair("--apply", "--clear-unproven-brp")
    version_count = PartCustomsDataVersion.objects.filter(part_type=bronco).count()
    output = _run_repair("--apply", "--clear-unproven-brp")
    assert "tier1_applied: 0" in output
    assert "tier2_applied: 0" in output
    assert PartCustomsInfo.objects.get(part_type=bronco).manufacturer == "BRONCO"
    assert PartCustomsDataVersion.objects.filter(part_type=bronco).count() == version_count


def test_repair_never_rewrites_the_frozen_historical_version(env):
    """Раньше проданная деталь: старая версия сохраняет "BRP" как исторический
    факт - ремонт живой карточки её не переписывает."""
    part = _stale_brp_part(env, name="BRONCO ДЕТАЛЬ", article="AT-08776",
                            manufacturer_name="BRONCO")
    _sell(env, part, number="AT-08776")
    original_version = PartCustomsDataVersion.objects.get(part_type=part, version=1)
    assert original_version.manufacturer == "BRP"

    _run_repair("--apply")

    original_version.refresh_from_db()
    assert original_version.manufacturer == "BRP"  # неизменна
    # Historical export row for the ALREADY-SOLD line still resolves correctly
    # (via authoritative_manufacturer on the frozen version), independent of
    # the live-card repair.
    row = _row_for(historical_customs_rows(), "AT-08776")
    assert row["manufacturer"] == "BRONCO"


def test_repair_never_touches_stock_sale_repair_or_prices(env):
    part = _stale_brp_part(env, name="BRONCO ДЕТАЛЬ", article="AT-08776",
                            manufacturer_name="BRONCO")
    _sell(env, part, number="AT-08776")
    part.refresh_from_db()
    before = (
        StockMovement.objects.count(), Sale.objects.count(), RepairOrder.objects.count(),
        part.recommended_price,
    )
    _run_repair("--apply", "--clear-unproven-brp")
    part.refresh_from_db()
    after = (
        StockMovement.objects.count(), Sale.objects.count(), RepairOrder.objects.count(),
        part.recommended_price,
    )
    assert before == after


# --- 13: History/Excel ordering parity at equal timestamps --------------------


def test_history_and_excel_use_the_same_tie_break_for_equal_timestamps(env):
    from apps.actions.customs_history import line_chronological_key

    first = _stale_brp_part(env, name="ПЕРВАЯ", article="TIE-A", manufacturer_name="BRP")
    second = _stale_brp_part(env, name="ВТОРАЯ", article="TIE-B", manufacturer_name="BRP")
    action_first = _sell(env, first, number="TIE-A")
    action_second = _sell(env, second, number="TIE-B")
    Sale.objects.filter(pk__in=[action_first.sale_id, action_second.sale_id]).update(
        sold_at=action_first.sale.sold_at
    )

    history_order = [row["number"] for row in customs_sources()]
    excel_order = [row["number"] for row in historical_customs_rows()]
    assert history_order == excel_order == ["TIE-A", "TIE-B"]

    # And the canonical key itself is the single source both call.
    line_a = {"occurred_at": action_first.sale.sold_at, "kind": "sale",
              "line_id": action_first.sale.lines.get().pk}
    line_b = {"occurred_at": action_second.sale.sold_at, "kind": "sale",
              "line_id": action_second.sale.lines.get().pk}
    assert line_chronological_key(line_a) < line_chronological_key(line_b)


# --- 14: Russian name precedence -----------------------------------------------


def test_russian_name_precedence_explicit_confirmed_wins_over_manual_fallback(env):
    part = create_manual_part(
        name="Гильза маслонасоса", article="10F", price="1000", manufacturer_name="BRP",
    )
    _receive(env, part)
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="ПОДТВЕРЖДЁННОЕ НАЗВАНИЕ", customs_name_ru_confirmed=True,
        customs_name_en="", manufacturer="BRP", country_of_origin="CANADA",
        gross_weight_kg=Decimal("0.250"), net_weight_kg=Decimal("0.200"),
        customs_unit_price_usd=Decimal("10.00"),
        application_area=ApplicationArea.SNOWMOBILE,
    )
    _sell(env, part, number="10F")
    row = _row_for(historical_customs_rows(), "10F")
    # Явно подтверждённое название сильнее собственного name детали.
    assert row["name_ru"] == "ПОДТВЕРЖДЁННОЕ НАЗВАНИЕ"
    assert row["name_ru_confirmed"] is True


def test_russian_name_precedence_manual_fallback_only_when_nothing_else_confirmed(env):
    part = create_manual_part(name="Гильза маслонасоса", article="10F", price="1000")
    _receive(env, part)
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="", customs_name_ru_confirmed=False,
        customs_name_en="", manufacturer="", country_of_origin="",
        gross_weight_kg=Decimal("0.250"), net_weight_kg=Decimal("0.200"),
        customs_unit_price_usd=Decimal("10.00"),
        application_area=ApplicationArea.SNOWMOBILE,
    )
    _sell(env, part, number="10F")
    row = _row_for(historical_customs_rows(), "10F")
    assert row["name_ru"] == "ГИЛЬЗА МАСЛОНАСОСА"
    assert row["name_ru_confirmed"] is False
