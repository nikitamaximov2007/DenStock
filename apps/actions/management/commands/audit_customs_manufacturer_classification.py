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
from apps.catalog.models import PartType
from apps.catalog.services import MANUAL_CATEGORY_NAME
from apps.inventory.presentation import part_exact_number

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


def _bucket_for(resolved: str) -> str:
    normalized = _normalized_manufacturer(resolved)
    for bucket, name in _NAME_BY_BUCKET.items():
        if normalized == name:
            return bucket
    return "unknown_manual" if not resolved else "other"


def classify_part(part: PartType) -> dict:
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
    number = part_exact_number(part, default="")
    resolved = catalog_or_explicit_manufacturer(part, number)
    bucket = _bucket_for(resolved)
    info = getattr(part, "customs_info", None)
    declared = (info.manufacturer.strip().upper() if info is not None else "")
    live = authoritative_manufacturer(part, declared, number) if info is not None else ""
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
        "usable_manual_name_ru": manual_part_name_ru(part),
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
        parts = (
            PartType.objects.select_related("category", "manufacturer", "customs_info")
            .prefetch_related("numbers")
            .order_by("pk")
        )
        rows = [classify_part(part) for part in parts]

        manual_rows = [row for row in rows if row["is_manual"]]
        with_info = [row for row in rows if row["has_customs_info"]]
        bucket_counts = {bucket: 0 for bucket in _BUCKET_LABELS}
        for row in rows:
            bucket_counts[row["bucket"]] += 1

        currently_marked_brp = [row for row in with_info if row["currently_marked_brp"]]
        misclassified = [row for row in with_info if row["classification_would_change"]]
        currently_eligible = [row for row in with_info if row["currently_eligible"]]
        should_be_eligible = [row for row in rows if row["should_be_eligible"]]
        stale_high = [row for row in with_info if row["stale_brp_high_confidence"]]
        stale_ambiguous = [row for row in with_info if row["stale_brp_ambiguous"]]
        name_gap = [
            row for row in with_info
            if not row["name_ru_declared"] and row["usable_manual_name_ru"]
        ]

        payload = {
            "part_types_total": len(rows),
            "manual_part_types_total": len(manual_rows),
            "manual_with_customs_info": sum(1 for row in manual_rows if row["has_customs_info"]),
            "parts_with_customs_info": len(with_info),
            **{f"bucket_{bucket}": bucket_counts[bucket] for bucket in _BUCKET_LABELS},
            "currently_marked_brp": len(currently_marked_brp),
            "proven_brp": bucket_counts["brp"],
            "currently_customs_export_eligible": len(currently_eligible),
            "should_be_customs_export_eligible": len(should_be_eligible),
            "classification_would_change": len(misclassified),
            "manual_currently_marked_brp_unproven": sum(
                1 for row in currently_marked_brp
                if row["is_manual"] and row["bucket"] != "brp"
            ),
            # Repair-таргеты: repair_customs_manufacturers --apply трогает
            # ТОЛЬКО stale_brp_high_confidence; stale_brp_ambiguous остаётся
            # в базе как есть и требует --clear-unproven для очистки.
            "stale_brp_high_confidence": len(stale_high),
            "stale_brp_ambiguous_needs_owner_review": len(stale_ambiguous),
            "export_rows_missing_name_ru_with_usable_manual_name": len(name_gap),
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

        limit = options["list_limit"]
        if limit:
            unproven_manual_brp = [
                row for row in currently_marked_brp
                if row["is_manual"] and row["bucket"] != "brp"
            ]
            self._list_section(
                "Ручные карточки, сейчас помеченные BRP без доказательства "
                "(кандидаты на ручной пересмотр; НЕ трогать автоматически)",
                unproven_manual_brp, limit,
            )
            self._list_section(
                "Строки, чья классификация изменилась бы (declared != resolved)",
                misclassified, limit,
            )
            self._list_section(
                "Строки с готовым ручным русским названием, но пустым customs_name_ru",
                name_gap, limit,
            )

    def _list_section(self, title, rows, limit):
        if not rows:
            return
        self.stdout.write("")
        self.stdout.write(f"{title} ({len(rows)}):")
        for row in rows[:limit]:
            part = row["part"]
            article = part.numbers.all()[0].value if part.numbers.all() else "-"
            self.stdout.write(
                f"  PartType #{part.pk} [{article}]: "
                f"declared={row['declared_manufacturer'] or '-'} "
                f"resolved={row['resolved_manufacturer'] or '-'} bucket={row['bucket']}"
            )
        if len(rows) > limit:
            self.stdout.write(f"  ... и ещё {len(rows) - limit}")
