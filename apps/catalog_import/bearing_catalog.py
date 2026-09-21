"""Safe plan and explicit apply path for the supplied bearing price list.

The source gives a purchase/procurement price in RUB. It is stored in the
manual purchase-price source and the current customer price is derived as
purchase price x 1.40. The list is a catalog import, not an inventory receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from apps.catalog.manual_pricing import (
    customer_price_from_purchase_price,
    set_manual_purchase_price,
)
from apps.catalog.models import Manufacturer, PartType, normalize_number
from apps.catalog.services import ManualPartError, create_manual_part
from apps.inventory.presentation import EXACT_NUMBER_KINDS

PRICE_SEMANTICS_UNCONFIRMED = "unconfirmed"
PRICE_SEMANTICS_PURCHASE = "purchase_cost_rub"


@dataclass(frozen=True)
class BearingSourceRow:
    brand: str
    article: str
    price_rub: Decimal

    @property
    def purchase_price_rub(self) -> Decimal:
        """The supplied RUB number, confirmed as purchase cost by the owner."""
        return self.price_rub

    @property
    def name(self) -> str:
        return f"Подшипник {self.brand} {self.article}"

    @property
    def identity(self) -> tuple[str, str]:
        return self.brand.casefold(), normalize_number(self.article)


@dataclass
class BearingPlanRow:
    source: BearingSourceRow
    status: str
    existing_part_ids: tuple[int, ...] = ()
    detail: str = ""


@dataclass
class BearingImportPlan:
    rows: list[BearingPlanRow] = field(default_factory=list)
    price_semantics: str = PRICE_SEMANTICS_UNCONFIRMED

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(row.status == status for row in self.rows)
            for status in ("CREATE", "ALREADY_EXISTS", "AMBIGUOUS")
        }

    @property
    def can_apply(self) -> bool:
        return (
            self.price_semantics == PRICE_SEMANTICS_PURCHASE
            and self.counts["AMBIGUOUS"] == 0
        )

    def as_summary(self) -> dict:
        return {
            "rows": len(self.rows),
            **self.counts,
            "price_semantics": self.price_semantics,
            "price_semantics_confirmed": self.price_semantics == PRICE_SEMANTICS_PURCHASE,
            "barcodes_created": 0,
            "stock_changes": False,
            "analog_links_created": 0,
            "rows_detail": [
                {
                    "row": index,
                    "brand": item.source.brand,
                    "article": item.source.article,
                    "purchase_price_rub": str(item.source.purchase_price_rub),
                    "customer_price_rub": str(
                        customer_price_from_purchase_price(item.source.purchase_price_rub)
                    ),
                    "status": item.status,
                    "existing_part_ids": list(item.existing_part_ids),
                    "detail": item.detail,
                }
                for index, item in enumerate(self.rows, start=1)
            ],
        }


# The Cyrillic C and K in rows 12 and 32 are intentional source characters.
BEARING_SOURCE_ROWS = (
    BearingSourceRow("FAG", "6012", Decimal("1800")),
    BearingSourceRow("FAG", "6305", Decimal("700")),
    BearingSourceRow("FAG", "6304", Decimal("670")),
    BearingSourceRow("FAG", "6201-C-2HRS", Decimal("250")),
    BearingSourceRow("FAG", "6205-C-2HRS", Decimal("460")),
    BearingSourceRow("FAG", "6005-C-2HRS", Decimal("650")),
    BearingSourceRow("FAG", "6203-C-2HRS", Decimal("400")),
    BearingSourceRow("FAG", "6010-2RSR", Decimal("1000")),
    BearingSourceRow("FAG", "6008", Decimal("720")),
    BearingSourceRow("FAG", "6008-2RSR", Decimal("850")),
    BearingSourceRow("FAG", "6006-2RSR", Decimal("750")),
    BearingSourceRow("FAG", "6006-C-2HRS-С3", Decimal("750")),
    BearingSourceRow("FAG", "6004-2RSR", Decimal("600")),
    BearingSourceRow("FAG", "6004-C-2Z", Decimal("580")),
    BearingSourceRow("FAG", "6304-2RSR", Decimal("700")),
    BearingSourceRow("FAG", "6303-2RSR", Decimal("550")),
    BearingSourceRow("FAG", "6210-2RSR", Decimal("1400")),
    BearingSourceRow("FAG", "30303-2RSR", Decimal("1700")),
    BearingSourceRow("FAG", "6403", Decimal("900")),
    BearingSourceRow("INA", "205-XL-NPP-B", Decimal("1800")),
    BearingSourceRow("INA", "UC205", Decimal("1400")),
    BearingSourceRow("FAG", "32207", Decimal("1300")),
    BearingSourceRow("FAG", "6000", Decimal("210")),
    BearingSourceRow("INA", "HK0912", Decimal("230")),
    BearingSourceRow("FAG", "6007", Decimal("600")),
    BearingSourceRow("FAG", "6207 N", Decimal("650")),
    BearingSourceRow("FAG", "30207-XL", Decimal("1350")),
    BearingSourceRow("FAG", "6206-C-2Z-C3", Decimal("450")),
    BearingSourceRow("FAG", "32206-XL", Decimal("1250")),
    BearingSourceRow("ZWZ", "6005-2RS", Decimal("350")),
    BearingSourceRow("ZWZ", "30303", Decimal("700")),
    BearingSourceRow("INA", "К25X29X13", Decimal("590")),
    BearingSourceRow("INA", "HK1816", Decimal("320")),
    BearingSourceRow("KOYO", "6206YR18LT-9T2CS44", Decimal("2000")),
    BearingSourceRow("KOYO", "6207 NRLT-9TC4", Decimal("2000")),
    BearingSourceRow("KOYO", "6208/X2BYR1NRLTHRZ", Decimal("6300")),
    BearingSourceRow("KOYO", "DG3278JS0-9TCS33", Decimal("4800")),
    BearingSourceRow("KOYO", "DAC286142", Decimal("1600")),
)


def _manufacturer_matches(brand: str):
    return list(Manufacturer.objects.filter(name__iexact=brand).order_by("pk"))


def _part_matches(row: BearingSourceRow) -> list[PartType]:
    return list(
        PartType.objects.filter(
            manufacturer__name__iexact=row.brand,
            numbers__normalized_value=normalize_number(row.article),
            numbers__kind__in=EXACT_NUMBER_KINDS,
        )
        .distinct()
        .order_by("pk")
    )


def build_plan(
    rows: tuple[BearingSourceRow, ...] = BEARING_SOURCE_ROWS,
    *,
    price_semantics: str = PRICE_SEMANTICS_UNCONFIRMED,
) -> BearingImportPlan:
    """Classify every source row without writing any database record."""
    planned: list[BearingPlanRow] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row.identity in seen:
            planned.append(BearingPlanRow(row, "AMBIGUOUS", detail="Повторяется строка источника."))
            continue
        seen.add(row.identity)

        manufacturers = _manufacturer_matches(row.brand)
        parts = _part_matches(row)
        manufacturer_case_mismatch = any(item.name != row.brand for item in manufacturers)
        if len(manufacturers) > 1 or len(parts) > 1 or manufacturer_case_mismatch:
            planned.append(
                BearingPlanRow(
                    row,
                    "AMBIGUOUS",
                    tuple(part.pk for part in parts),
                    "Найдено несколько совпадений производителя или детали.",
                )
            )
        elif parts:
            planned.append(
                BearingPlanRow(
                    row,
                    "ALREADY_EXISTS",
                    (parts[0].pk,),
                    "Точная пара производитель + артикул уже есть.",
                )
            )
        else:
            planned.append(
                BearingPlanRow(
                    row,
                    "CREATE",
                    detail=(
                        "Производитель будет создан." if not manufacturers else ""
                    ),
                )
            )
    return BearingImportPlan(planned, price_semantics=price_semantics)


@transaction.atomic
def apply_plan(plan: BearingImportPlan) -> dict:
    """Apply only an explicitly purchase-price-confirmed, unambiguous plan."""
    if plan.price_semantics != PRICE_SEMANTICS_PURCHASE:
        raise ManualPartError(
            "Импорт остановлен: смысл RUB-цен не подтверждён как закупочная цена."
        )
    if plan.counts["AMBIGUOUS"]:
        raise ManualPartError("Импорт остановлен: сначала разберите неоднозначные строки.")

    created = 0
    existing = 0
    for item in plan.rows:
        if item.status == "ALREADY_EXISTS":
            existing += 1
            continue
        if _part_matches(item.source):
            raise ManualPartError(
                f"Строка «{item.source.name}» изменилась после проверки. Повторите dry-run."
            )
        part = create_manual_part(
            name=item.source.name,
            article=item.source.article,
            price=None,
            manufacturer_name=item.source.brand,
        )
        set_manual_purchase_price(part, item.source.purchase_price_rub)
        created += 1
        if part.barcodes.exists():  # defensive invariant, never a source value
            raise ManualPartError("Импорт создал неожиданный штрихкод.")
    return {
        "created": created,
        "already_exists": existing,
        "barcodes_created": 0,
        "stock_changes": False,
        "analog_links_created": 0,
    }
