"""Shared fixtures for Search 2.0 tests (portable and PostgreSQL modules)."""
from decimal import Decimal

from apps.actions.models import PartCustomsInfo
from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit

WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "TRUNCATE", "ALTER", "DROP", "CREATE")


class Catalog:
    """Tiny builder so each test states only the facts it depends on."""

    def __init__(self):
        self.category = Category.objects.create(name="Search tests")
        self.manufacturer = Manufacturer.objects.create(name="SEARCH-MAN")
        self.unit = Unit.objects.get(name="Штука")

    def part(self, name, *, article=None, kind=PartNumber.Kind.OEM, active=True, price="100"):
        part = PartType.objects.create(
            name=name, category=self.category, manufacturer=self.manufacturer,
            unit=self.unit, tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal(price) if price is not None else None,
            is_active=active,
        )
        if article is not None:
            PartNumber.objects.create(part=part, value=article, kind=kind, is_primary=True)
        return part

    def russian(self, part, name, *, confirmed=True):
        return PartCustomsInfo.objects.create(
            part_type=part, customs_name_ru=name, customs_name_ru_confirmed=confirmed,
        )


def assert_no_writes(captured):
    """Transaction control (BEGIN/COMMIT/SAVEPOINT) and SET are not data writes."""
    writes = [
        query["sql"] for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith(WRITE_PREFIXES)
    ]
    assert not writes, f"search wrote to the database: {writes[:3]}"
