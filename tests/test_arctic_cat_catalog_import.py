"""Arctic Cat adapter: source facts only, through the normal import workflow."""

from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from apps.catalog.models import PartNumber, PartType
from apps.catalog.services import create_manual_part
from apps.catalog_import.models import ArcticCatCatalogPart, CatalogImportBatch
from apps.catalog_import.services import CatalogImportError, apply_batch, run_check, save_upload
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement

HEADERS = ["P/N", "Description", "Pkg Qty", "DEALER PRICE"]
PASSWORD = "arctic-test-password"


def _workbook(rows, *, sheet="usprice", headers=HEADERS):
    book = Workbook()
    page = book.active
    page.title = sheet
    page.append(headers)
    for row in rows:
        page.append(row)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def _upload(rows, **kwargs):
    return SimpleUploadedFile(
        "ARCTIC CAT DEALER 2026.xlsx",
        _workbook(rows, **kwargs),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@pytest.fixture
def boss(client, django_user_model):
    user = django_user_model.objects.create_superuser("arctic-boss", password=PASSWORD)
    client.force_login(user)
    return client


def _send(client, rows, **kwargs):
    return client.post(
        reverse("catalog_import_upload"),
        {"catalog": "arctic_cat", "workbook": _upload(rows, **kwargs)},
        follow=True,
    )


def _apply(client, rows, **kwargs):
    _send(client, rows, **kwargs)
    batch = CatalogImportBatch.objects.latest("pk")
    client.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)
    batch.refresh_from_db()
    assert batch.status == CatalogImportBatch.Status.APPLIED, batch.error_text
    return batch


def _stock_snapshot():
    return (
        StockLot.objects.count(),
        StockBalance.objects.count(),
        PartItem.objects.count(),
        StockMovement.objects.count(),
    )


def test_valid_usprice_is_dry_run_then_creates_private_source_facts_only(boss):
    before = _stock_snapshot()
    response = _send(boss, [["0101-045", "KEY, TORQUE BRK", "5", "1.61"]])

    batch = CatalogImportBatch.objects.get()
    assert response.status_code == 200
    assert batch.catalog == CatalogImportBatch.Catalog.ARCTIC_CAT
    assert batch.status == CatalogImportBatch.Status.CHECKED
    assert batch.summary["sheet"] == "usprice"
    assert batch.summary["new"] == 1
    assert PartType.objects.count() == 0

    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)
    entry = ArcticCatCatalogPart.objects.select_related("part").get()
    assert entry.supplier_article == "0101-045"
    assert entry.source_description == "KEY, TORQUE BRK"
    assert entry.package_quantity == "5"
    assert entry.dealer_price_usd == Decimal("1.61")
    assert entry.part.recommended_price is None
    assert not entry.part.is_public
    assert entry.part.numbers.get(kind=PartNumber.Kind.ARTICLE).value == "0101-045"
    assert _stock_snapshot() == before


def test_numeric_cell_with_safe_excel_format_keeps_leading_zero_article(boss):
    book = Workbook()
    page = book.active
    page.title = "usprice"
    page.append(HEADERS)
    page.append([101045, "KEY", "5", "1.61"])
    page.cell(2, 1).number_format = "0000-000"
    output = BytesIO()
    book.save(output)
    boss.post(
        reverse("catalog_import_upload"),
        {
            "catalog": "arctic_cat",
            "workbook": SimpleUploadedFile("arctic.xlsx", output.getvalue()),
        },
        follow=True,
    )
    batch = CatalogImportBatch.objects.get()
    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)
    assert ArcticCatCatalogPart.objects.get().supplier_article == "0101-045"


@pytest.mark.parametrize(
    ("price", "state", "counter"),
    [("0", "zero", "zero_price_rows"), ("", "blank", "blank_price_rows")],
)
def test_zero_or_blank_dealer_price_is_unavailable_not_a_customer_price(
    boss, price, state, counter
):
    batch = _apply(boss, [["0101-045", "KEY", "5", price]])
    entry = ArcticCatCatalogPart.objects.select_related("part").get()
    assert batch.apply_summary[counter] == 1
    assert entry.dealer_price_usd is None
    assert entry.dealer_price_state == state
    assert entry.part.recommended_price is None


def test_unavailable_reimport_never_erases_a_known_positive_supplier_price(boss):
    _apply(boss, [["0101-045", "KEY", "5", "1.61"]])
    _apply(boss, [["0101-045", "KEY", "5", "0"]])
    entry = ArcticCatCatalogPart.objects.get()
    assert entry.dealer_price_usd == Decimal("1.61")
    assert entry.dealer_price_state == "known"


def test_exact_replacement_is_stored_without_creating_missing_target(boss):
    _apply(boss, [["0101-058", "R/B 0101-055", "1", "0"]])
    entry = ArcticCatCatalogPart.objects.get()
    assert entry.replacement_article == "0101-055"
    assert entry.normalized_replacement_article == "0101055"
    assert ArcticCatCatalogPart.objects.count() == 1
    assert not ArcticCatCatalogPart.objects.filter(supplier_article="0101-055").exists()


def test_malformed_replacement_is_a_warning_and_stays_plain_description(boss):
    response = _send(boss, [["0101-058", "R/B 0101-055 EXTRA", "1", "0"]])
    batch = CatalogImportBatch.objects.get()
    assert response.status_code == 200
    assert batch.status == CatalogImportBatch.Status.CHECKED
    assert batch.summary["warnings"] == 1
    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)
    entry = ArcticCatCatalogPart.objects.get()
    assert entry.source_description == "R/B 0101-055 EXTRA"
    assert entry.replacement_article == ""


def test_duplicates_are_reported_and_conflicts_are_never_arbitrarily_applied(boss):
    _send(
        boss,
        [
            ["0101-045", "KEY A", "5", "1.61"],
            ["0101-045", "KEY B", "5", "2.00"],
        ],
    )
    batch = CatalogImportBatch.objects.get()
    assert batch.summary["duplicate_part_numbers"] == 1
    assert batch.summary["errors"] == 1
    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)
    assert ArcticCatCatalogPart.objects.get().source_description == "KEY A"


def test_changes_are_classified_and_repeated_file_is_idempotent(boss):
    _apply(boss, [["0101-045", "OLD", "5", "1.61"]])
    changed = _apply(boss, [["0101-045", "R/B 0101-055", "10", "2.00"]])
    assert changed.apply_summary["existing"] == 1
    assert changed.apply_summary["description_changed"] == 1
    assert changed.apply_summary["package_quantity_changed"] == 1
    assert changed.apply_summary["price_changed"] == 1
    assert changed.apply_summary["replacement_changed"] == 1

    repeat = _apply(boss, [["0101-045", "R/B 0101-055", "10", "2.00"]])
    assert repeat.apply_summary["unchanged"] == 1
    assert ArcticCatCatalogPart.objects.count() == 1


def test_same_article_in_another_catalog_is_not_merged(boss):
    manual = create_manual_part(
        name="Manual Arctic-like article",
        article="0101-045",
        manufacturer_name="Another manufacturer",
    )
    _apply(boss, [["0101-045", "Arctic source", "1", "1.61"]])
    entry = ArcticCatCatalogPart.objects.select_related("part").get()
    assert entry.part_id != manual.pk
    assert PartType.objects.filter(numbers__normalized_value="0101045").distinct().count() == 2


def test_search_finds_source_article_by_exact_normalized_partial_and_description(boss):
    _apply(boss, [["0101-045", "TORQUE KEY", "1", "1.61"]])
    for query in ("0101-045", "0101045", "0101", "TORQUE"):
        response = boss.get(reverse("part_search"), {"q": query})
        assert "TORQUE KEY" in response.content.decode()


def test_stale_dry_run_is_rejected(boss):
    _send(boss, [["0101-045", "KEY", "1", "1.61"]])
    batch = CatalogImportBatch.objects.get()
    _apply(boss, [["0101-046", "OTHER", "1", "2.00"]])
    with pytest.raises(CatalogImportError, match="STALE_DRY_RUN"):
        apply_batch(batch)


def test_wrong_schema_can_be_inspected_without_any_catalog_write(boss):
    _send(boss, [["0101-045", "KEY"]], sheet="Price", headers=["Article", "Name"])
    batch = CatalogImportBatch.objects.get()
    assert batch.status == CatalogImportBatch.Status.CHECK_FAILED
    response = boss.get(reverse("catalog_import_inspect", args=[batch.pk]))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Price" in body
    assert "P/N" in body
    assert "Отсутствуют обязательные заголовки" in body
    assert not ArcticCatCatalogPart.objects.exists()


def test_broken_workbook_is_rejected_without_source_rows(boss):
    response = boss.post(
        reverse("catalog_import_upload"),
        {
            "catalog": "arctic_cat",
            "workbook": SimpleUploadedFile("broken.xlsx", b"not an xlsx archive"),
        },
        follow=True,
    )
    assert "не читается" in response.content.decode().lower()
    assert not ArcticCatCatalogPart.objects.exists()


def test_upload_requires_same_permission_as_existing_catalog_import(client, django_user_model):
    user = django_user_model.objects.create_user("seller", password=PASSWORD)
    client.force_login(user)
    response = _send(client, [["0101-045", "KEY", "1", "1.61"]])
    assert response.status_code == 403
    assert not CatalogImportBatch.objects.exists()


def test_private_upload_and_service_apply_have_no_public_url_or_direct_stock_write(
    boss, settings, tmp_path
):
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    upload = _upload([["0101-045", "KEY", "1", "1.61"]])
    batch = run_check(save_upload(upload, catalog="arctic_cat"))
    assert batch.stored_path and "/" not in batch.stored_path
    before = _stock_snapshot()
    apply_batch(batch)
    assert _stock_snapshot() == before
