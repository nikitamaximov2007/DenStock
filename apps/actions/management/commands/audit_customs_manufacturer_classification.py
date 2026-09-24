"""Read-only аудит производителя и допуска к BRP/PRO-X таможенной выгрузке.

    python manage.py audit_customs_manufacturer_classification
    python manage.py audit_customs_manufacturer_classification --json
    python manage.py audit_customs_manufacturer_classification --list 50

Команда НИЧЕГО не пишет: ни в PartType, ни в PartCustomsInfo, ни в
PartCustomsDataVersion. Она отвечает на один вопрос — можно ли доверять
текущей классификации производителя — и раскладывает каждую деталь с
таможенной карточкой на категории из плана backfill'а:

    A. proven BRP        - доказанная связь с каталогом BRP (BrpPartLink)
    B. proven PRO-X       - явный производитель PROX/PRO-X (каталог или карточка)
    C. proven BRONCO      - явный производитель BRONCO
    D. proven SPI         - явный производитель SPI
    E. proven MOTUL       - явный производитель MOTUL
    F. unknown/manual     - производитель не доказан вообще

«Proven» здесь означает то же самое, что доказывает система при сохранении
формы (``apps.inventory.presentation.manufacturer_display``): связь с
каталогом поставщика (BRP/Polaris/aftermarket) или явно выбранный
``PartType.manufacturer``. Открытие карточки или сохранение без явного выбора
производителя никогда не доказывает BRP - раньше именно это было источником
дефекта (модель по умолчанию хранила «BRP»).

«Misclassified» - сохранённое значение ``PartCustomsInfo.manufacturer``
(то, что реально уйдёт в Excel/заказ) расходится с доказанной категорией.
Такие строки чаще всего - наследие старого default'а и кандидаты на
последующий ручной аудит/backfill (в этой RC-задаче backfill не выполняется).
"""
import json

from django.core.management.base import BaseCommand

from apps.actions.services import (
    _normalized_manufacturer,
    authoritative_manufacturer,
    catalog_or_explicit_manufacturer,
    is_brp_export_eligible,
    manual_part_name_ru,
)
from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.catalog.models import PartNumber, PartType, normalize_number
from apps.catalog.services import MANUAL_CATEGORY_NAME
from apps.catalog_import.models import AftermarketCatalogPart
from apps.inventory.presentation import part_exact_number
from apps.polaris.models import PolarisCatalogPart, PolarisPartLink

_BUCKET_LABELS = {
    "brp": "A. proven BRP",
    "pro_x": "B. proven PRO-X",
    "bronco": "C. proven BRONCO",
    "spi": "D. proven SPI",
    "motul": "E. proven MOTUL",
    "unknown_manual": "F. unknown/manual",
    "other": "other named brand (вне области этой выгрузки)",
}
_NAME_BY_BUCKET = {
    "brp": "BRP", "pro_x": "PROX", "bronco": "BRONCO", "spi": "SPI", "motul": "MOTUL",
}


class ClassificationFacts:
    """Bulk read-only manufacturer evidence for the whole catalog."""

    def __init__(self):
        self.brp_by_part = dict(
            BrpPartLink.objects.values_list("part_id", "brp_part__material_no")
        )
        self.polaris_by_part = dict(
            PolarisPartLink.objects.values_list("part_id", "polaris_part__part_number")
        )
        self.aftermarket_by_part = {}
        self.aftermarket_by_number = {}
        aftermarket_rows = AftermarketCatalogPart.objects.order_by("pk").values_list(
            "part_id", "normalized_manufacturer_number", "manufacturer__name"
        )
        for part_id, number, manufacturer in aftermarket_rows:
            self.aftermarket_by_part[part_id] = manufacturer
            self.aftermarket_by_number.setdefault(number, manufacturer)
        self.brp_numbers = set(
            BrpCatalogPart.objects.filter(is_current=True).values_list(
                "material_no_norm", flat=True
            )
        )
        self.polaris_numbers = set(
            PolarisCatalogPart.objects.values_list("part_number_norm", flat=True)
        )
        self.exact_numbers = {}
        exact_numbers = PartNumber.objects.filter(
            kind__in=(PartNumber.Kind.OEM, PartNumber.Kind.ARTICLE)
        ).order_by("-is_primary", "pk").values_list("part_id", "value")
        for part_id, value in exact_numbers:
            self.exact_numbers.setdefault(part_id, value)

    def number_for(self, part) -> str:
        if part.pk in self.brp_by_part:
            return self.brp_by_part[part.pk]
        if part.pk in self.polaris_by_part:
            return self.polaris_by_part[part.pk]
        return self.exact_numbers.get(part.pk, "")

    def has_direct_catalog(self, part) -> bool:
        return (
            part.pk in self.brp_by_part
            or part.pk in self.polaris_by_part
            or part.pk in self.aftermarket_by_part
        )

    def resolved_manufacturer(self, part, number: str) -> str:
        if part.pk in self.brp_by_part:
            return "BRP"
        if part.pk in self.polaris_by_part:
            return "POLARIS"
        if part.pk in self.aftermarket_by_part:
            return self.aftermarket_by_part[part.pk].strip().upper()
        normalized = normalize_number(number)
        if normalized in self.brp_numbers:
            return "BRP"
        if normalized in self.polaris_numbers:
            return "POLARIS"
        if normalized in self.aftermarket_by_number:
            return self.aftermarket_by_number[normalized].strip().upper()
        return (part.manufacturer.name if part.manufacturer_id else "").strip().upper()


def _bucket_for(resolved: str) -> str:
    normalized = _normalized_manufacturer(resolved)
    for bucket, name in _NAME_BY_BUCKET.items():
        if normalized == name:
            return bucket
    return "unknown_manual" if not resolved else "other"


def classify_part(part: PartType, *, facts: ClassificationFacts | None = None) -> dict:
    """Доказанная категория детали, живое чтение и сохранённая карточка.

    Три разных значения, три разных вопроса:

    * ``resolved`` - что доказывает СВЕЖАЯ проверка каталога/карточки прямо
      сейчас, независимо от того, что сохранено (``catalog_or_explicit_manufacturer``).
      Определяет bucket (A-F) и «should_be_*».
    * ``declared`` - что БУКВАЛЬНО лежит в живой таможенной карточке
      (``PartCustomsInfo.manufacturer``) без какой-либо перепроверки. Только
      для «currently_marked_brp» - состояние данных как есть, до любого чтения.
    * ``live`` - что реально увидит сотрудник и что реально попадёт в
      Excel/заказ ПРЯМО СЕЙЧАС, без единой записи в базу: тот же
      ``authoritative_manufacturer``, что использует History/Excel/заказ.
      Для declared="BRP" без доказательства live отличается от declared -
      устаревший default уже не побеждает при чтении. Для любого другого
      declared live совпадает с declared (см. authoritative_manufacturer:
      небрендовый default не существовал).
    """
    is_manual = part.category.name == MANUAL_CATEGORY_NAME
    number = facts.number_for(part) if facts is not None else part_exact_number(part, default="")
    resolved = (
        facts.resolved_manufacturer(part, number)
        if facts is not None
        else catalog_or_explicit_manufacturer(part, number)
    )
    bucket = _bucket_for(resolved)
    info = getattr(part, "customs_info", None)
    declared = (info.manufacturer.strip().upper() if info is not None else "")
    if info is None:
        live = ""
    elif facts is not None:
        # authoritative_manufacturer() only re-proves the legacy BRP default;
        # every other declared value is already authoritative.  Keep the bulk
        # path query-free while preserving that exact read-time rule.
        live = resolved if _normalized_manufacturer(declared) == "BRP" else declared
    else:
        live = authoritative_manufacturer(part, declared, number)
    stale_brp = _normalized_manufacturer(declared) == "BRP" and declared != live
    return {
        "part": part,
        "is_manual": is_manual,
        "resolved_manufacturer": resolved,
        "declared_manufacturer": declared,
        "live_manufacturer": live,
        "bucket": bucket,
        "currently_marked_brp": _normalized_manufacturer(declared) == "BRP",
        "should_be_brp": bucket == "brp",
        # "Currently eligible" - то, что экспорт/заказ реально допускают СЕЙЧАС
        # (через authoritative_manufacturer), а не сырое сохранённое значение:
        # устаревший default больше не проходит проверку допуска при чтении.
        "currently_eligible": is_brp_export_eligible(live),
        "should_be_eligible": is_brp_export_eligible(resolved),
        "classification_would_change": (
            _normalized_manufacturer(declared) != _normalized_manufacturer(resolved)
        ),
        # Устаревший default, который чтение уже перепроверяет и не путает с
        # доказанным BRP, но который ещё стоит поправить в самой карточке
        # (см. repair_customs_manufacturers), чтобы следующая настоящая
        # правка формы не заморозила его в новую версию как «BRP».
        "stale_brp": stale_brp,
        "stale_brp_high_confidence": stale_brp and bool(resolved),
        "stale_brp_ambiguous": stale_brp and not resolved,
        "has_customs_info": info is not None,
        "name_ru_declared": (info.customs_name_ru.strip() if info is not None else ""),
        "usable_manual_name_ru": (
            part.name.strip()
            if facts is not None and is_manual and not facts.has_direct_catalog(part)
            else manual_part_name_ru(part)
        ),
        "article": number,
    }


class Command(BaseCommand):
    help = (
        "Read-only аудит производителя и допуска к BRP/PRO-X таможенной выгрузке. "
        "Ничего не пишет; для backfill'а нужна отдельная, явно подтверждённая команда."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json", action="store_true", dest="as_json", help="Вывести итоги как JSON.",
        )
        parser.add_argument(
            "--list", type=int, default=20, dest="list_limit",
            help="Сколько ID/артикулов на категорию показывать (0 - не показывать).",
        )

    def handle(self, *args, **options):
        facts = ClassificationFacts()
        parts = (
            PartType.objects.select_related("category", "manufacturer", "customs_info")
            .order_by("pk")
        )
        limit = options["list_limit"]
        samples = {"unproven_manual_brp": [], "misclassified": [], "name_gap": []}
        totals = {key: 0 for key in samples}
        manual_part_types_total = 0
        manual_with_customs_info = 0
        parts_with_customs_info = 0
        bucket_counts = {bucket: 0 for bucket in _BUCKET_LABELS}
        currently_marked_brp = 0
        currently_eligible = 0
        should_be_eligible = 0
        stale_high = 0
        stale_ambiguous = 0

        def collect(key, row):
            totals[key] += 1
            if len(samples[key]) < limit:
                samples[key].append(row)

        for part in parts.iterator(chunk_size=1000):
            row = classify_part(part, facts=facts)
            bucket_counts[row["bucket"]] += 1
            manual_part_types_total += row["is_manual"]
            parts_with_customs_info += row["has_customs_info"]
            manual_with_customs_info += row["is_manual"] and row["has_customs_info"]
            should_be_eligible += row["should_be_eligible"]
            if row["has_customs_info"]:
                currently_marked_brp += row["currently_marked_brp"]
                currently_eligible += row["currently_eligible"]
                stale_high += row["stale_brp_high_confidence"]
                stale_ambiguous += row["stale_brp_ambiguous"]
                if row["classification_would_change"]:
                    collect("misclassified", row)
                if not row["name_ru_declared"] and row["usable_manual_name_ru"]:
                    collect("name_gap", row)
                if row["currently_marked_brp"] and row["is_manual"] and row["bucket"] != "brp":
                    collect("unproven_manual_brp", row)

        payload = {
            "part_types_total": sum(bucket_counts.values()),
            "manual_part_types_total": manual_part_types_total,
            "manual_with_customs_info": manual_with_customs_info,
            "parts_with_customs_info": parts_with_customs_info,
            **{f"bucket_{bucket}": bucket_counts[bucket] for bucket in _BUCKET_LABELS},
            "currently_marked_brp": currently_marked_brp,
            "proven_brp": bucket_counts["brp"],
            "currently_customs_export_eligible": currently_eligible,
            "should_be_customs_export_eligible": should_be_eligible,
            "classification_would_change": totals["misclassified"],
            "manual_currently_marked_brp_unproven": totals["unproven_manual_brp"],
            # Repair-таргеты: repair_customs_manufacturers --apply трогает
            # ТОЛЬКО stale_brp_high_confidence; stale_brp_ambiguous остаётся
            # в базе как есть и требует --clear-unproven для очистки.
            "stale_brp_high_confidence": stale_high,
            "stale_brp_ambiguous_needs_owner_review": stale_ambiguous,
            "export_rows_missing_name_ru_with_usable_manual_name": totals["name_gap"],
        }

        if options["as_json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            self.stdout.write("Категории (доказанный производитель):")
            for bucket, label in _BUCKET_LABELS.items():
                self.stdout.write(f"  {label}: {bucket_counts[bucket]}")
            self.stdout.write("")
            for key, value in payload.items():
                self.stdout.write(f"{key}: {value}")

        if limit:
            self._list_section(
                "Ручные карточки, сейчас помеченные BRP без доказательства "
                "(кандидаты на ручной пересмотр; НЕ трогать автоматически)",
                samples["unproven_manual_brp"], totals["unproven_manual_brp"], limit,
            )
            self._list_section(
                "Строки, чья классификация изменилась бы (declared != resolved)",
                samples["misclassified"], totals["misclassified"], limit,
            )
            self._list_section(
                "Строки с готовым ручным русским названием, но пустым customs_name_ru",
                samples["name_gap"], totals["name_gap"], limit,
            )

    def _list_section(self, title, rows, total, limit):
        if not total:
            return
        self.stdout.write("")
        self.stdout.write(f"{title} ({total}):")
        for row in rows[:limit]:
            part = row["part"]
            article = row["article"] or "-"
            self.stdout.write(
                f"  PartType #{part.pk} [{article}]: "
                f"declared={row['declared_manufacturer'] or '-'} "
                f"resolved={row['resolved_manufacturer'] or '-'} bucket={row['bucket']}"
            )
        if total > len(rows):
            self.stdout.write(f"  ... и ещё {total - len(rows)}")
