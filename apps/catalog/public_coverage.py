"""Read-only content coverage of the public catalog.

Answers "how complete is what customers see" with the same rules the public
pages use: visibility (``public_parts``), confirmed Russian names, published
photos, confirmed analog links, canonical availability and the canonical
current price. It never writes and never refreshes a cache; on PostgreSQL the
whole report runs inside a READ ONLY transaction.

Coverage gaps are content work, not launch blockers; see
docs/operations/public-catalog-launch-data-quality.md.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from django.db import connection, transaction
from django.db.models import Q

from apps.actions.models import PartCustomsInfo
from apps.inventory.movement import live_stock_rows

from .models import PartNumber, PartType, PublicPartPhoto
from .public_catalog import APPLICATIONS, confirmed_links, public_parts

ZERO = Decimal("0")
TOP_MANUFACTURERS = 15


@dataclass
class CoverageReport:
    total_parts: int = 0
    public_parts: int = 0
    hidden_parts: int = 0
    retired_parts: int = 0
    with_confirmed_ru: int = 0
    without_confirmed_ru: int = 0
    with_published_photo: int = 0
    without_photo: int = 0
    with_confirmed_analogs: int = 0
    confirmed_originals: int = 0
    confirmed_analogs: int = 0
    in_stock: int = 0
    zero_stock: int = 0
    price_known: int = 0
    price_unknown: int = 0
    with_article: int = 0
    without_article: int = 0
    with_manufacturer: int = 0
    without_manufacturer: int = 0
    top_manufacturers: list[tuple[str, int]] = field(default_factory=list)
    applications: dict[str, int] = field(default_factory=dict)
    without_application: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _count(queryset) -> int:
    return queryset.values("pk").distinct().count()


def _collect() -> CoverageReport:
    visible = public_parts()
    visible_ids = visible.values("pk")
    report = CoverageReport(
        total_parts=PartType.objects.count(),
        public_parts=visible.count(),
        hidden_parts=PartType.objects.filter(is_public=False).count(),
        retired_parts=PartType.objects.filter(is_public=True, is_active=False).count(),
    )

    report.with_confirmed_ru = (
        PartCustomsInfo.objects.filter(part_type__in=visible_ids, customs_name_ru_confirmed=True)
        .exclude(customs_name_ru__regex=r"^\s*$")
        .values("part_type")
        .distinct()
        .count()
    )
    report.without_confirmed_ru = report.public_parts - report.with_confirmed_ru

    report.with_published_photo = (
        PublicPartPhoto.objects.filter(
            part__in=visible_ids, status=PublicPartPhoto.Status.PUBLISHED
        )
        .values("part")
        .distinct()
        .count()
    )
    report.without_photo = report.public_parts - report.with_published_photo

    originals, analogs = set(), set()
    for original_id, analog_id in confirmed_links().values_list("original_id", "analog_id"):
        originals.add(original_id)
        analogs.add(analog_id)
    report.confirmed_originals = len(originals)
    report.confirmed_analogs = len(analogs)
    report.with_confirmed_analogs = len(originals | analogs)

    # Canonical availability for every part that physically has stock; any
    # other public part is zero by definition. Stock rows are few compared
    # with catalog rows, so this never loads the catalog itself.
    available: Counter = Counter()
    for row in live_stock_rows():
        available[row.part_type.pk] += row.available
    stocked = {part_id for part_id, quantity in available.items() if quantity > ZERO}
    report.in_stock = visible.filter(pk__in=stocked).count() if stocked else 0
    report.zero_stock = report.public_parts - report.in_stock

    report.price_known = visible.filter(recommended_price__gt=0).count()
    report.price_unknown = report.public_parts - report.price_known

    with_article = visible.filter(
        Q(brp_link__isnull=False)
        | Q(polaris_link__isnull=False)
        | Q(
            numbers__kind__in=[PartNumber.Kind.OEM, PartNumber.Kind.ARTICLE],
            numbers__value__regex=r"\S",
        )
    )
    report.with_article = _count(with_article)
    report.without_article = report.public_parts - report.with_article

    # Same precedence as manufacturer_display: BRP link, Polaris link, card.
    brp = visible.filter(brp_link__isnull=False)
    polaris = visible.filter(brp_link__isnull=True, polaris_link__isnull=False)
    by_card = (
        visible.filter(brp_link__isnull=True, polaris_link__isnull=True, manufacturer__isnull=False)
        .values_list("manufacturer__name")
        .order_by()
    )
    makers = Counter({"BRP": brp.count(), "POLARIS": polaris.count()})
    for (name,) in by_card.iterator(chunk_size=5000):
        makers[name] += 1
    makers = +makers
    report.with_manufacturer = sum(makers.values())
    report.without_manufacturer = report.public_parts - report.with_manufacturer
    report.top_manufacturers = makers.most_common(TOP_MANUFACTURERS)

    explicit = Counter()
    rows = PartCustomsInfo.objects.filter(part_type__in=visible_ids).exclude(application_area="")
    for (area,) in rows.values_list("application_area").iterator(chunk_size=5000):
        value = (area or "").strip().upper()
        if value in APPLICATIONS:
            explicit[value] += 1
    report.applications = {value: explicit.get(value, 0) for value in APPLICATIONS}
    report.without_application = report.public_parts - sum(explicit.values())
    return report


def collect_coverage() -> CoverageReport:
    """Build the report without writing anything, even by accident."""
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
        report = _collect()
        transaction.set_rollback(True)
    return report


@dataclass(frozen=True, slots=True)
class RussianNameBacklogRow:
    """Одна публичная деталь, которой не хватает подтверждённого названия."""

    part_id: int
    article: str
    manufacturer: str
    english_name: str
    available: Decimal
    in_stock: bool


def russian_name_backlog() -> list[RussianNameBacklogRow]:
    """Очередь на перевод: публичные детали без подтверждённого названия.

    Сначала то, что лежит на складе и по убыванию остатка: покупатель ищет по
    русскому названию именно то, что можно купить сегодня. Только чтение.
    """
    from apps.inventory.presentation import (
        manufacturer_display,
        part_exact_number,
        with_part_identity,
    )

    confirmed = set(
        PartCustomsInfo.objects.filter(customs_name_ru_confirmed=True)
        .exclude(customs_name_ru="")
        .values_list("part_type_id", flat=True)
    )
    available: Counter = Counter()
    for row in live_stock_rows():
        available[row.part_type.pk] += row.available

    rows = []
    queryset = public_parts().exclude(pk__in=confirmed)
    for part in with_part_identity(queryset, part_field="").iterator(chunk_size=2000):
        quantity = available.get(part.pk, ZERO)
        rows.append(
            RussianNameBacklogRow(
                part_id=part.pk,
                article=part_exact_number(part, default=""),
                manufacturer=manufacturer_display(part),
                english_name=part.name,
                available=quantity,
                in_stock=quantity > ZERO,
            )
        )
    rows.sort(key=lambda row: (not row.in_stock, -row.available, row.part_id))
    return rows
