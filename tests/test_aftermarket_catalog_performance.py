"""Large-file qualification for the known dealer workbook shape."""

from openpyxl import Workbook

from apps.catalog.models import Unit
from apps.catalog_import.aftermarket_catalog import build_plan


def _large_workbook(path, rows: int):
    book = Workbook(write_only=True)
    sheet = book.create_sheet("priceupdate")
    sheet.append(
        ["Manufacturer", "Item SKU", "Manufacturer Number", "Description", "MSRP", "Dlr Cost"]
    )
    sheet.append(["", "", "", "", "", ""])
    for number in range(rows):
        sheet.append(
            ["40 BELOW", f"SKU-{number:06d}", f"SM-{number:06d}", "Part", "101.95", "63.31"]
        )
    book.save(path)


def test_130k_supplier_rows_are_previewed_with_bounded_queries(tmp_path, db):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    path = tmp_path / "aftermarket-130k.xlsx"
    _large_workbook(path, 130_000)

    with CaptureQueriesContext(connection) as queries:
        plan = build_plan(path).as_summary()

    assert plan["rows_scanned"] == 130_000
    assert plan["blank_rows"] == 1
    assert plan["new_parts"] == 130_000
    # Article matching is chunked for SQLite's bind limit, never one query per
    # workbook row.
    assert len(queries) < 300
