from decimal import Decimal

from openpyxl import Workbook

from apps.brp.correction import apply_zero_wholesale_correction, plan_zero_wholesale_correction
from apps.brp.models import BrpCatalogPart
from apps.catalog_import.adapters import file_sha256
from apps.catalog_import.models import CatalogImportBatch
from apps.inventory.models import StockMovement

HEADERS = [
    "Material_No", "Part_Desc", "Last_Yr_Util", "Status",
    "РОЗНИЦА", "ОПТОВАЯ", "ЗАМЕНА НОМЕРА", "ЗАМЕНА НОМЕРА",
]


def _workbook(rows, tmp_path, *, name):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    sheet.append([""] * len(HEADERS))
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    return path


def _applied_source_batch(path, settings, tmp_path):
    root = tmp_path / "private" / "catalog-imports"
    root.mkdir(parents=True)
    stored = root / "current.xlsx"
    stored.write_bytes(path.read_bytes())
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    return CatalogImportBatch.objects.create(
        catalog="brp",
        status=CatalogImportBatch.Status.APPLIED,
        source_filename="current.xlsx",
        source_sha256=file_sha256(stored),
        source_size=stored.stat().st_size,
        stored_path=stored.name,
    )


def test_correction_retains_old_prices_without_rewriting_new_metadata(db, settings, tmp_path):
    previous = _workbook(
        [
            ["460061", "OLD", "", "", "137.99", "112.67", "", ""],
            ["NO-PRICE", "OLD", "", "", "0", "0", "", ""],
        ],
        tmp_path,
        name="previous.xlsx",
    )
    current = _workbook(
        [
            ["460061", "NEW", "", "VIN", "0", "0", "456", ""],
            ["NO-PRICE", "NEW", "", "USE", "0", "0", "", ""],
        ],
        tmp_path,
        name="current.xlsx",
    )
    BrpCatalogPart.objects.create(
        material_no="460061", part_desc="NEW", brp_status="VIN", replacement_no_1="456",
        wholesale_price_usd=Decimal("0"),
    )
    BrpCatalogPart.objects.create(
        material_no="NO-PRICE", part_desc="NEW", brp_status="USE", wholesale_price_usd=Decimal("0")
    )
    source = _applied_source_batch(current, settings, tmp_path)

    movements_before = StockMovement.objects.count()
    dry, selected = plan_zero_wholesale_correction(source, previous_file=previous)
    assert dry.previous_catalog_fallback == 1
    assert dry.no_usable_price == 1
    assert selected == {"460061": Decimal("112.67"), "NO-PRICE": None}

    correction, summary = apply_zero_wholesale_correction(source, previous_file=previous)
    assert correction.status == CatalogImportBatch.Status.APPLIED
    assert correction.summary["kind"] == "brp_zero_wholesale_fallback_v1"
    assert summary.rows_to_update == 2
    restored = BrpCatalogPart.objects.get(material_no="460061")
    assert restored.wholesale_price_usd == Decimal("112.67")
    assert restored.part_desc == "NEW"
    assert restored.brp_status == "VIN"
    assert restored.replacement_no_1 == "456"
    assert BrpCatalogPart.objects.get(material_no="NO-PRICE").wholesale_price_usd is None
    assert StockMovement.objects.count() == movements_before


def test_correction_second_run_is_idempotent(db, settings, tmp_path):
    previous = _workbook(
        [["M", "OLD", "", "", "10", "8", "", ""]], tmp_path, name="previous.xlsx"
    )
    current = _workbook(
        [["M", "NEW", "", "", "0", "0", "", ""]], tmp_path, name="current.xlsx"
    )
    BrpCatalogPart.objects.create(material_no="M", wholesale_price_usd=Decimal("0"))
    source = _applied_source_batch(current, settings, tmp_path)
    apply_zero_wholesale_correction(source, previous_file=previous)
    _correction, second = apply_zero_wholesale_correction(source, previous_file=previous)
    assert second.rows_to_update == 0


def test_correction_uses_bounded_database_batches(db, settings, tmp_path):
    rows = [
        [f"M-{number:04}", "OLD", "", "", "10", "8", "", ""]
        for number in range(1005)
    ]
    previous = _workbook(rows, tmp_path, name="previous.xlsx")
    current = _workbook(
        [[row[0], "NEW", "", "", "0", "0", "", ""] for row in rows],
        tmp_path,
        name="current.xlsx",
    )
    BrpCatalogPart.objects.bulk_create(
        [BrpCatalogPart(material_no=row[0], wholesale_price_usd=Decimal("0")) for row in rows]
    )
    source = _applied_source_batch(current, settings, tmp_path)
    summary, selected = plan_zero_wholesale_correction(source, previous_file=previous)
    assert summary.rows_to_update == 1005
    assert len(selected) == 1005
