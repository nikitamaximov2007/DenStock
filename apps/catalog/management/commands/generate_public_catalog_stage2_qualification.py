"""Create the disposable Stage 2 Search 2.0 qualification corpus."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.actions.models import PartCustomsInfo
from apps.catalog.models import Category, PartNumber, PartType, Unit, normalize_number

SEED_ROWS = (
    ("420-892-388", "ARTICLE FIXTURE", "АРТИКУЛ ФИКСТУРЫ"),
    ("Q-BRG-01", "BEARING", "ПОДШИПНИК"),
    ("Q-BLT-01", "DRIVE BELT", "РЕМЕНЬ"),
    ("Q-WHL-01", "WHEEL", "КОЛЕСО"),
    ("Q-GSK-01", "GASKET FIXTURE", "ПРОКЛАДКА"),
    ("BERRNG-01", "ARTICLE RANKING FIXTURE", "РЕЙТИНГ ФИКСТУРЫ"),
    # These remain isolated exact-hit probes when the large corpus deliberately
    # has many BEARING/ПРОКЛАДКА candidates for partial and fuzzy workloads.
    ("Q-EN-EXACT-01", "QUALIFICATION EXACT EN NAME", "УНИКАЛЬНОЕ ТОЧНОЕ РУ НАЗВАНИЕ"),
)
UNCONFIRMED_SEED = ("Q-RU-UNC-01", "UNCONFIRMED FIXTURE", "ПРОКЛАДКА")


def _bulk_number(part, value):
    """Reproduce PartNumber.save's only derived field for bulk creation."""
    return PartNumber(
        part=part,
        value=value,
        normalized_value=normalize_number(value),
        kind=PartNumber.Kind.ARTICLE,
        is_primary=True,
    )


class Command(BaseCommand):
    help = "Create a disposable valid 125k Stage 2 public-search qualification corpus."

    def add_arguments(self, parser):
        parser.add_argument("--size", type=int, default=125_000)
        parser.add_argument("--batch-size", type=int, default=5_000)
        parser.add_argument("--confirm-isolated", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_isolated"]:
            raise CommandError("Refusing to create qualification data without --confirm-isolated.")
        size = options["size"]
        batch_size = options["batch_size"]
        if size < len(SEED_ROWS) + 1:
            raise CommandError(f"--size must be at least {len(SEED_ROWS) + 1}.")
        if batch_size < 1:
            raise CommandError("--batch-size must be positive.")
        if PartType.objects.exists():
            raise CommandError("Qualification database must have no PartType rows.")

        category, _ = Category.objects.get_or_create(name="Stage 2 qualification")
        unit, _ = Unit.objects.get_or_create(name="шт", defaults={"short_name": "шт"})
        confirmed_target = round(size * 0.9)

        with transaction.atomic():
            for article, english, russian in SEED_ROWS:
                part = PartType.objects.create(
                    name=english,
                    category=category,
                    unit=unit,
                    tracking_mode=PartType.TrackingMode.BULK,
                    is_active=True,
                )
                PartNumber.objects.create(
                    part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
                )
                if russian:
                    PartCustomsInfo.objects.create(
                        part_type=part,
                        customs_name_ru=russian,
                        customs_name_ru_confirmed=True,
                    )
            article, english, russian = UNCONFIRMED_SEED
            part = PartType.objects.create(
                name=english,
                category=category,
                unit=unit,
                tracking_mode=PartType.TrackingMode.BULK,
                is_active=True,
            )
            PartNumber.objects.create(
                part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
            )
            PartCustomsInfo.objects.create(
                part_type=part, customs_name_ru=russian, customs_name_ru_confirmed=False
            )

            seeded_confirmed = sum(russian is not None for _article, _english, russian in SEED_ROWS)
            for offset in range(len(SEED_ROWS) + 1, size, batch_size):
                stop = min(offset + batch_size, size)
                parts = [
                    PartType(
                        name=(
                            f"BEARING DRIVE {index:06d}"
                            if index % 10
                            else f"GASKET ASSEMBLY {index:06d}"
                        ),
                        category=category,
                        unit=unit,
                        tracking_mode=PartType.TrackingMode.BULK,
                        is_active=True,
                    )
                    for index in range(offset, stop)
                ]
                PartType.objects.bulk_create(parts, batch_size=batch_size)
                PartNumber.objects.bulk_create(
                    [
                        _bulk_number(part, f"ART-{index:06d}-XY")
                        for index, part in enumerate(parts, offset)
                    ],
                    batch_size=batch_size,
                )
                customs = []
                for index, part in enumerate(parts, offset):
                    confirmed = seeded_confirmed < confirmed_target
                    customs.append(
                        PartCustomsInfo(
                            part_type=part,
                            customs_name_ru=(
                                f"ПРОКЛАДКА ГОЛОВКИ {index:06d}"
                                if confirmed
                                else f"НЕПОДТВЕРЖДЕННАЯ ПРОКЛАДКА {index:06d}"
                            ),
                            customs_name_ru_confirmed=confirmed,
                        )
                    )
                    seeded_confirmed += confirmed
                PartCustomsInfo.objects.bulk_create(customs, batch_size=batch_size)

        parts = PartType.objects.count()
        numbers = PartNumber.objects.count()
        confirmed = PartCustomsInfo.objects.filter(customs_name_ru_confirmed=True).count()
        unconfirmed = PartCustomsInfo.objects.filter(customs_name_ru_confirmed=False).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {parts} Parts, {numbers} PartNumbers, {confirmed} confirmed RU "
                f"and {unconfirmed} unconfirmed RU rows."
            )
        )
