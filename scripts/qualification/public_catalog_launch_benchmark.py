"""Launch regression on the 125k Stage 2 corpus: search vs catalog vs page.

Reuses the valid Stage 2 corpus and its twelve benchmark terms, adds the
launch-stage data the new pages read (stock, confirmed analogs, published
photos, manufacturers, application areas) and measures, per term:

* ``search``   - Search 2.0 identities only (``search_part_ids``);
* ``catalog``  - the public read service (visibility, filters, facets and
                 hydration of one page), which includes the search;
* ``filtered`` - the same with in-stock, analog and manufacturer filters;
* ``page``     - the full public HTML response through the public
                 middleware stack, which includes all of the above.

    DATABASE_URL=postgres://user:pass@127.0.0.1:PORT/launch_corpus \\
        python scripts/qualification/public_catalog_launch_benchmark.py \\
        --confirm-isolated --expect-database launch_corpus --enrich --output out.json

Only loopback PostgreSQL databases with the expected name are accepted.
``--enrich`` writes synthetic launch data into that disposable corpus; the
measurement phase itself is checked to be read-only.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import sys
import time
from decimal import Decimal
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "qualification"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
BUSINESS_TABLES = (
    "catalog_parttype",
    "catalog_partnumber",
    "catalog_partanalog",
    "catalog_publicpartphoto",
    "actions_partcustomsinfo",
    "inventory_stocklot",
    "sales_reservation",
    "sales_reservationline",
)
HOST = "catalog.benchmark"
ENRICH_STOCK = 300
ENRICH_ANALOGS = 400
ENRICH_PHOTOS = 300
ENRICH_APPLICATIONS = 5000
MANUFACTURERS = ("WISECO", "PROX", "ATHENA", "VERTEX", "EBC", "SPI", "EPI", "NAMURA")


def _guard(args):
    from django.conf import settings
    from django.db import connection

    database = settings.DATABASES["default"]
    if not args.confirm_isolated:
        raise SystemExit("Refusing to run without --confirm-isolated.")
    if connection.vendor != "postgresql":
        raise SystemExit("This harness needs PostgreSQL 16.")
    if (database.get("HOST") or "") not in LOOPBACK:
        raise SystemExit(f"Refusing a non-loopback database host: {database.get('HOST')!r}")
    if database.get("NAME") != args.expect_database:
        raise SystemExit(
            f"Connected to {database.get('NAME')!r}, expected {args.expect_database!r}."
        )


def _counts():
    from django.db import connection

    with connection.cursor() as cursor:
        result = {}
        for table in BUSINESS_TABLES:
            cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
            result[table] = cursor.fetchone()[0]
    return result


def enrich():
    """Add launch-stage rows to the disposable corpus through canonical services."""
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.db import connection, transaction
    from django.utils import timezone
    from PIL import Image

    from apps.actions.models import PartCustomsInfo
    from apps.catalog.models import Manufacturer, PartAnalog, PartType, PublicPartPhoto
    from apps.catalog.public_photos import publish_photo
    from apps.core.images import add_image
    from apps.inventory.services import create_stock_lot, receive_stock_lot
    from apps.procurement.models import Batch, BatchLine
    from apps.procurement.services import finalize_cost
    from apps.suppliers.models import Supplier
    from apps.warehouse.models import StorageLocation

    if PartType.objects.count() < 125_000:
        raise SystemExit("Enrichment expects the 125k Stage 2 corpus.")
    if PublicPartPhoto.objects.exists() or PartAnalog.objects.exists():
        print("corpus already enriched")
        return

    user = get_user_model().objects.create_user(username="launch-benchmark")
    supplier = Supplier.objects.create(name="Benchmark supplier")
    cell = StorageLocation.objects.create(
        name="Benchmark cell", code="S09-D09-C09", storage_allowed=True, is_active=True
    )
    makers = [Manufacturer.objects.get_or_create(name=name)[0].pk for name in MANUFACTURERS]
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE catalog_parttype SET manufacturer_id = (%s::bigint[])[(id %% %s) + 1], "
            "recommended_price = CASE WHEN id %% 11 = 0 THEN NULL ELSE 100 + id %% 9000 END",
            [makers, len(makers)],
        )

    bearings = list(
        PartType.objects.filter(name__startswith="BEARING DRIVE")
        .order_by("pk")
        .values_list("pk", flat=True)[:2000]
    )
    gaskets = list(
        PartType.objects.filter(name__startswith="GASKET ASSEMBLY")
        .order_by("pk")
        .values_list("pk", flat=True)[:2000]
    )
    with transaction.atomic():
        for part_id in bearings[:ENRICH_STOCK]:
            part = PartType.objects.get(pk=part_id)
            batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
            line = BatchLine.objects.create(
                batch=batch, part_type=part, quantity=Decimal("3"), unit_cost_currency=Decimal("10")
            )
            batch.status = Batch.Status.ACCEPTED
            batch.save(update_fields=["status"])
            finalize_cost(batch, user)
            line.refresh_from_db()
            receive_stock_lot(create_stock_lot(line, cell, Decimal("3")), by=user)
        now = timezone.now()
        PartAnalog.objects.bulk_create(
            [
                PartAnalog(
                    original_id=original,
                    analog_id=analog,
                    is_confirmed=index % 4 != 0,
                    confirmed_at=now if index % 4 != 0 else None,
                    confirmed_by=user if index % 4 != 0 else None,
                    created_by=user,
                )
                for index, (original, analog) in enumerate(
                    zip(gaskets[:ENRICH_ANALOGS], bearings[:ENRICH_ANALOGS], strict=True)
                )
            ]
        )
        PartCustomsInfo.objects.filter(part_type_id__in=bearings[:ENRICH_APPLICATIONS]).update(
            application_area="ГИДРОЦИКЛ"
        )

    buffer = BytesIO()
    Image.new("RGB", (1600, 1200), (0, 104, 163)).save(buffer, "JPEG", quality=88)
    payload = buffer.getvalue()
    for part_id in bearings[:ENRICH_PHOTOS]:
        part = PartType.objects.get(pk=part_id)
        image = add_image(
            part.images,
            image=SimpleUploadedFile("bench.jpg", payload, content_type="image/jpeg"),
            caption="",
            by=user,
        )
        publish_photo(image, source="own", by=user)
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")
    print("enriched corpus")


def _public_settings():
    from django.conf import settings
    from django.test import override_settings

    from apps.catalog.public_settings import PUBLIC_CONTEXT_PROCESSORS, PUBLIC_SETTINGS

    templates = copy.deepcopy(settings.TEMPLATES)
    templates[0]["OPTIONS"]["context_processors"] = list(PUBLIC_CONTEXT_PROCESSORS)
    return override_settings(
        **{**PUBLIC_SETTINGS, "TEMPLATES": templates, "ALLOWED_HOSTS": [HOST], "DEBUG": False}
    )


def _timed(fn, samples, warmup=5):
    for _ in range(warmup):
        fn()
    values = []
    for _ in range(samples):
        started = time.perf_counter()
        fn()
        values.append((time.perf_counter() - started) * 1000)
    values.sort()
    return {
        "median": round(statistics.median(values), 3),
        "p95": round(values[max(0, int(len(values) * 0.95) - 1)], 3),
        "max": round(values[-1], 3),
    }


def measure(samples):
    from django.test import Client
    from public_catalog_stage2_benchmark import CASES

    from apps.catalog.public_catalog import search_catalog
    from apps.catalog.search import search_part_ids

    client = Client(HTTP_HOST=HOST)
    rows = []
    for number, label, query, _target, _tier in CASES:
        result = search_catalog(query, {})
        manufacturer = result.cards[0].facts.manufacturer if result.cards else ""
        row = {
            "case": number,
            "class": label,
            "query": query,
            "hits": result.ranked_total,
            "search": _timed(lambda q=query: search_part_ids(q), samples),
            "catalog": _timed(lambda q=query: search_catalog(q, {}), samples),
            "filtered": _timed(
                lambda q=query, m=manufacturer: search_catalog(
                    q, {"in_stock": "1", "relation": "analog", "manufacturer": m}
                ),
                samples,
            ),
            "page": _timed(lambda q=query: client.get("/search/", {"q": q}).content, samples),
        }
        if result.cards:
            path = f"/parts/{result.cards[0].facts.public_id}/"
            row["detail"] = _timed(lambda p=path: client.get(p).content, samples)
        rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-isolated", action="store_true")
    parser.add_argument("--expect-database", required=True)
    parser.add_argument("--enrich", action="store_true")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    import django

    django.setup()
    _guard(args)
    if args.enrich:
        enrich()
    before = _counts()
    with _public_settings():
        rows = measure(args.samples)
    after = _counts()
    evidence = {
        "cases": rows,
        "counts_before": before,
        "counts_after": after,
        "pure_read": before == after,
    }
    columns = ("search", "catalog", "filtered", "page", "detail")
    print(f"{'#':>2} {'class':<26} {'hits':>5} " + " ".join(f"{c:>9}" for c in columns))
    for row in rows:
        detail = row.get("detail", {}).get("median", "")
        print(
            f"{row['case']:>2} {row['class']:<26} {row['hits']:>5} "
            f"{row['search']['median']:>9} {row['catalog']['median']:>9} "
            f"{row['filtered']['median']:>9} {row['page']['median']:>9} {detail:>9}"
        )
    print("pure read:", evidence["pure_read"])
    if args.output:
        Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["pure_read"] else 1


if __name__ == "__main__":
    sys.exit(main())
