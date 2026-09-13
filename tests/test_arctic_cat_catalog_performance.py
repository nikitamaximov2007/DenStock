"""Bounded-query smoke test for the Arctic Cat adapter."""

import time

from django.db import connection
from django.test.utils import CaptureQueriesContext
from openpyxl import Workbook

from apps.catalog_import.arctic_cat_catalog import apply_file, build_plan
from apps.catalog_import.models import ArcticCatCatalogPart

ROWS = 1500


def _workbook(tmp_path, rows=ROWS):
    book = Workbook(write_only=True)
    page = book.create_sheet("usprice")
    page.append(["P/N", "Description", "Pkg Qty", "DEALER PRICE"])
    for index in range(rows):
        page.append(
            [
                f"0{index // 1000:03d}-{index % 1000:03d}",
                f"ARCTIC PART {index}",
                "1",
                "1.61",
            ]
        )
    path = tmp_path / "arctic-catalog.xlsx"
    book.save(path)
    return path


def test_large_arctic_workbook_uses_bounded_database_queries(db, tmp_path):
    path = _workbook(tmp_path)
    started = time.monotonic()
    with CaptureQueriesContext(connection) as captured:
        plan = build_plan(path)
    check_seconds = time.monotonic() - started
    check_queries = len(captured)

    started = time.monotonic()
    with CaptureQueriesContext(connection) as captured:
        result = apply_file(path)
    apply_seconds = time.monotonic() - started
    apply_queries = len(captured)

    assert plan.new == ROWS
    assert result["created_parts"] == ROWS
    assert ArcticCatCatalogPart.objects.count() == ROWS
    assert check_queries < ROWS / 10, check_queries
    assert apply_queries < ROWS / 10, apply_queries
    print(
        f"\nROWS={ROWS} check={check_seconds:.2f}s/{check_queries}q "
        f"apply={apply_seconds:.2f}s/{apply_queries}q"
    )
