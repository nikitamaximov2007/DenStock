from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError, OutputWrapper
from django.db import IntegrityError
from django.urls import reverse

from apps.brp.models import BrpPartLink
from apps.catalog.manual_pricing import (
    customer_price_from_purchase_price,
    set_manual_purchase_price,
)
from apps.catalog.models import (
    ManualPurchasePrice,
    Manufacturer,
    PartBarcode,
    PartNumber,
    PartType,
)
from apps.catalog.services import ManualPartError, create_manual_part
from apps.catalog_import.bearing_catalog import (
    BEARING_SOURCE_ROWS,
    PRICE_SEMANTICS_PURCHASE,
    PRICE_SEMANTICS_UNCONFIRMED,
    apply_plan,
    build_plan,
)
from apps.core.part_lookup import MatchSource, resolve_part_lookup
from apps.inventory.models import PartItem, StockLot, StockMovement


def test_source_has_all_38_rows_and_preserves_confusable_characters():
    assert len(BEARING_SOURCE_ROWS) == 38
    assert BEARING_SOURCE_ROWS[11].article == "6006-C-2HRS-С3"
    assert ord(BEARING_SOURCE_ROWS[11].article[-2]) == ord("С")
    assert BEARING_SOURCE_ROWS[31].article == "К25X29X13"
    assert ord(BEARING_SOURCE_ROWS[31].article[0]) == ord("К")


def test_dry_run_classifies_all_rows_without_writes(db):
    plan = build_plan()

    assert plan.counts == {"CREATE": 38, "ALREADY_EXISTS": 0, "AMBIGUOUS": 0}
    assert plan.price_semantics == PRICE_SEMANTICS_UNCONFIRMED
    assert not plan.can_apply
    assert set(Manufacturer.objects.values_list("name", flat=True)) == set()
    assert PartType.objects.count() == 0


def test_exact_manufacturer_and_article_is_reused_not_duplicated(db):
    row = BEARING_SOURCE_ROWS[0]
    existing = create_manual_part(
        name=row.name,
        article=row.article,
        price=row.price_rub,
        manufacturer_name=row.brand,
    )

    plan = build_plan(rows=(row,))

    assert plan.counts == {"CREATE": 0, "ALREADY_EXISTS": 1, "AMBIGUOUS": 0}
    assert plan.rows[0].existing_part_ids == (existing.pk,)


def test_same_article_under_same_manufacturer_is_ambiguous(db):
    row = BEARING_SOURCE_ROWS[0]
    create_manual_part(name="Первый", article=row.article, manufacturer_name=row.brand)
    create_manual_part(name="Второй", article=row.article, manufacturer_name=row.brand)

    plan = build_plan(rows=(row,))

    assert plan.counts == {"CREATE": 0, "ALREADY_EXISTS": 0, "AMBIGUOUS": 1}
    assert not plan.can_apply


def test_apply_requires_explicit_purchase_price_semantics(db):
    plan = build_plan(rows=(BEARING_SOURCE_ROWS[0],))

    with pytest.raises(ManualPartError, match="закупочная цена"):
        apply_plan(plan)

    assert PartType.objects.count() == 0


def test_confirmed_apply_stores_purchase_cost_and_derives_customer_price(db):
    plan = build_plan(
        price_semantics=PRICE_SEMANTICS_PURCHASE,
    )

    result = apply_plan(plan)

    assert result == {
        "created": 38,
        "already_exists": 0,
        "barcodes_created": 0,
        "stock_changes": False,
        "analog_links_created": 0,
    }
    assert PartType.objects.count() == 38
    assert PartNumber.objects.filter(kind=PartNumber.Kind.ARTICLE).count() == 38
    assert set(PartType.objects.values_list("manufacturer__name", flat=True)) == {
        "FAG", "INA", "ZWZ", "KOYO"
    }
    assert PartBarcode.objects.count() == 0
    assert PartItem.objects.count() == 0
    assert StockLot.objects.count() == 0
    assert StockMovement.objects.count() == 0
    assert not BrpPartLink.objects.exists()
    assert ManualPurchasePrice.objects.count() == 38
    for source in BEARING_SOURCE_ROWS:
        part = PartType.objects.get(name=source.name)
        purchase = ManualPurchasePrice.objects.get(part_type=part)
        assert purchase.purchase_price_rub == source.price_rub
        assert part.recommended_price == customer_price_from_purchase_price(source.price_rub)
        assert part.price_provenance == PartType.PriceProvenance.VALID_MANUAL_EXCEPTION


def test_manual_purchase_price_change_is_explicit_and_keeps_customer_formula(db):
    part = create_manual_part(name="Подшипник FAG 6012", article="6012")

    set_manual_purchase_price(part, 1800)
    part.refresh_from_db()
    assert part.recommended_price == 2520
    assert part.manual_purchase_price.purchase_price_rub == 1800

    set_manual_purchase_price(part, 2000)
    part.refresh_from_db()
    assert part.recommended_price == 2800
    assert ManualPurchasePrice.objects.get(part_type=part).purchase_price_rub == 2000


def test_barcode_is_a_string_with_leading_zero_and_multiple_values(db):
    part = create_manual_part(name="Подшипник FAG 6205-C-2HRS", article="6205-C-2HRS")
    first = PartBarcode.objects.create(part=part, value=" 001234567890\r\n")
    second = PartBarcode.objects.create(part=part, value="4012345678901")

    assert first.value == "001234567890"
    assert second.value == "4012345678901"
    result = resolve_part_lookup(" 001234567890\n")
    assert result.found
    assert result.candidate.part == part
    assert result.candidate.exact_number == "6205-C-2HRS"
    assert result.candidate.match_source == MatchSource.BARCODE


def test_barcode_case_variant_is_rejected_for_another_part(db):
    first = create_manual_part(name="Первая", article="FIRST")
    second = create_manual_part(name="Вторая", article="SECOND")
    PartBarcode.objects.create(part=first, value="ABC123")

    with pytest.raises(IntegrityError):
        PartBarcode.objects.create(part=second, value="abc123")


def test_barcode_card_action_is_csrf_protected_and_removal_keeps_part(
    db, client, django_user_model
):
    django_user_model.objects.create_superuser("bearing-admin", password="pass")
    client.login(username="bearing-admin", password="pass")
    part = create_manual_part(name="Деталь", article="D-1")

    detail = client.get(reverse("part_detail", args=[part.pk])).content.decode()
    assert "<th>Артикул</th>" in detail
    assert "D-1" in detail

    response = client.post(reverse("part_barcode_add", args=[part.pk]), {"value": "0007"})
    assert response.status_code == 302
    barcode = PartBarcode.objects.get()
    assert PartType.objects.get(pk=part.pk).numbers.get().value == "D-1"

    response = client.post(reverse("part_barcode_delete", args=[barcode.pk]))
    assert response.status_code == 302
    assert PartType.objects.filter(pk=part.pk).exists()
    assert not PartBarcode.objects.exists()


def test_command_defaults_to_read_only_and_apply_requires_purchase_confirmation(db):
    output = OutputWrapper(StringIO())
    call_command("import_bearings", stdout=output)
    assert "Режим проверки" in output.getvalue()
    assert PartType.objects.count() == 0

    with pytest.raises(CommandError, match="prices-are-purchase-cost"):
        call_command("import_bearings", "--apply")


def test_command_rejects_customer_price_confirmation(db):
    with pytest.raises(CommandError, match="закупочную стоимость"):
        call_command("import_bearings", "--apply", "--prices-are-customer-selling")
