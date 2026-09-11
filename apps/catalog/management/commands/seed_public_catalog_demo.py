"""Seed a disposable database with a small, realistic public-catalog demo.

For local review, the demo checklist, acceptance and load tests only. It
refuses to run without ``--confirm-isolated`` and on any database that already
has parts, so it cannot touch a copy of real data. Stock is created through
the canonical batch, lot, receipt and reservation services, never by writing
balances directly.
"""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageDraw

from apps.actions.models import PartCustomsInfo
from apps.catalog.models import (
    Category,
    Manufacturer,
    PartAnalog,
    PartNumber,
    PartType,
    Unit,
)
from apps.catalog.public_photos import publish_photo, reject_photo
from apps.core.images import add_image
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

# article, English name, confirmed RU (None: none; "?" prefix: unconfirmed),
# manufacturer, price (None: unknown), stock, unit, application.
DEMO_PARTS = (
    ("420892388", "PISTON ASS'Y WITH RINGS, 71.87 MM", "Поршень в сборе с кольцами, 71,87 мм",
     "BRP", "30047", "3", "Штука", "ГИДРОЦИКЛ"),
    ("010-921", "PISTON KIT SEA-DOO 1503 NA", None, "WSM", "18500", "5", "Штука", "ГИДРОЦИКЛ"),
    ("PX-01-1503", "PISTON KIT 1503 CAST", None, "PROX", "16900", "0", "Штука", ""),
    ("420931785", "DRIVE BELT", "Ремень вариатора", "BRP", None, "2", "Штука", "СНЕГОХОД"),
    ("715900111", "SPARK PLUG NGK", "?Свеча зажигания", "BRP", "950", "0", "Штука", ""),
    ("779133", "XPS 4-STROKE SYNTHETIC OIL 1L", "Масло XPS 4T синтетическое, 1 л", "BRP",
     "1890", "12", "Литр", "ГИДРОЦИКЛ"),
    ("25-1497", "BEARING 6205-2RS", None, "ALL BALLS RACING INC", "640", "10", "Штука",
     "КВАДРОЦИКЛ"),
    ("*USE 520SR05*    520SRO-MLJ",
     "EXTREMELY LONG CATALOG NAME FOR A CHAIN AND SPROCKET KIT WITH MANY WORDS",
     None, "JT CHAIN AND SPROCKETS", "12990", "0", "Комплект", ""),
)
FILLER_MAKERS = ("ATHENA", "VERTEX", "WISECO", "PROX", "EBC")


def _photo_bytes(label: str, color: tuple[int, int, int], size=(1600, 1200)) -> bytes:
    image = Image.new("RGB", size, (248, 250, 252))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((200, 200, size[0] - 200, size[1] - 200), radius=80, fill=color)
    draw.text((260, 260), label, fill=(255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class Command(BaseCommand):
    help = "Seed an EMPTY disposable database with the public catalog demo."

    def add_arguments(self, parser):
        parser.add_argument("--confirm-isolated", action="store_true")
        parser.add_argument("--filler", type=int, default=60)

    def handle(self, *args, **options):
        if not options["confirm_isolated"]:
            raise CommandError("Refusing to seed demo data without --confirm-isolated.")
        if settings.DENSTOCK_MODE in {"production", "public-catalog"}:
            raise CommandError("Refusing to seed demo data in a production runtime.")
        if PartType.objects.exists():
            raise CommandError("Demo database must have no PartType rows.")
        with transaction.atomic():
            summary = self._seed(options["filler"])
        self.stdout.write(self.style.SUCCESS(summary))

    def _seed(self, filler: int) -> str:
        user = get_user_model().objects.create_user(username="public-demo-seed")
        user.set_unusable_password()
        user.is_superuser = True
        user.save()
        category = Category.objects.create(name="Демо публичного каталога")
        supplier = Supplier.objects.create(name="Демо поставщик")
        location = StorageLocation.objects.create(
            name="Демо ячейка", code="S01-D01-C01", storage_allowed=True, is_active=True
        )
        parts = {}
        lots = {}
        for article, english, russian, maker, price, stock, unit, area in DEMO_PARTS:
            part = self._part(category, article, english, maker, price, unit)
            if russian:
                PartCustomsInfo.objects.create(
                    part_type=part,
                    customs_name_ru=russian.lstrip("?"),
                    customs_name_ru_confirmed=not russian.startswith("?"),
                    application_area=area,
                )
            elif area:
                PartCustomsInfo.objects.create(part_type=part, application_area=area)
            if Decimal(stock) > 0:
                lots[article] = self._stock(user, supplier, location, part, Decimal(stock))
            parts[article] = part

        original = parts["420892388"]
        confirmed = PartAnalog.objects.create(
            original=original, analog=parts["010-921"], source="internal", created_by=user
        )
        confirmed.is_confirmed = True
        confirmed.confirmed_at = timezone.now()
        confirmed.confirmed_by = user
        confirmed.save()
        # Unconfirmed on purpose: it must never reach the public pages.
        PartAnalog.objects.create(
            original=original, analog=parts["PX-01-1503"], source="internal", created_by=user
        )

        bearing_lot = lots["25-1497"]
        reservation = create_reservation(
            customer_name="Демо резерв", expires_at=timezone.now() + timedelta(days=3), by=user
        )
        add_stock_lot_to_reservation(reservation, bearing_lot, Decimal("4"), by=user)
        activate_reservation(reservation, by=user)

        published = self._photo(original, "420892388 main", (0, 104, 163), user)
        publish_photo(published, source="manufacturer", note="Каталог BRP", by=user)
        second = self._photo(original, "420892388 side", (0, 150, 200), user)
        publish_photo(second, source="own", by=user)
        self._photo(original, "candidate only", (120, 120, 120), user)
        rejected = self._photo(parts["010-921"], "wrong part", (180, 60, 60), user)
        reject_photo(rejected, by=user)
        analog_photo = self._photo(parts["010-921"], "010-921", (30, 120, 60), user)
        publish_photo(analog_photo, source="supplier", note="Прайс WSM", by=user)

        makers = {name: Manufacturer.objects.get_or_create(name=name)[0] for name in FILLER_MAKERS}
        unit = Unit.objects.get(name="Штука")
        for index in range(1, filler + 1):
            maker = makers[FILLER_MAKERS[index % len(FILLER_MAKERS)]]
            part = PartType.objects.create(
                name=f"GASKET KIT TOP END {index:03d}",
                category=category,
                manufacturer=maker,
                unit=unit,
                tracking_mode=PartType.TrackingMode.BULK,
                recommended_price=Decimal(1000 + index * 37) if index % 7 else None,
            )
            PartNumber.objects.create(
                part=part, value=f"GK-{index:04d}", kind=PartNumber.Kind.ARTICLE, is_primary=True
            )
            if index % 4 == 0:
                self._stock(user, supplier, location, part, Decimal(index % 9 + 1))

        retired = self._part(category, "RETIRED-1", "RETIRED GASKET", "VERTEX", "100", "Штука")
        retired.is_active = False
        retired.save(update_fields=["is_active"])
        hidden = self._part(category, "HIDDEN-1", "HIDDEN GASKET", "VERTEX", "100", "Штука")
        hidden.is_public = False
        hidden.save(update_fields=["is_public"])
        return (
            f"Seeded {PartType.objects.count()} parts, "
            f"{PartAnalog.objects.filter(is_confirmed=True).count()} confirmed analog link(s), "
            "3 published photos."
        )

    def _part(self, category, article, english, maker, price, unit_name):
        manufacturer = Manufacturer.objects.get_or_create(name=maker)[0]
        part = PartType.objects.create(
            name=english,
            category=category,
            manufacturer=manufacturer,
            unit=Unit.objects.get(name=unit_name),
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal(price) if price is not None else None,
        )
        PartNumber.objects.create(
            part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
        )
        return part

    def _stock(self, user, supplier, location, part, quantity):
        batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch, part_type=part, quantity=quantity, unit_cost_currency=Decimal("10")
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, user)
        line.refresh_from_db()
        lot = create_stock_lot(line, location, quantity)
        receive_stock_lot(lot, by=user)
        return lot

    def _photo(self, part, label, color, user):
        upload = SimpleUploadedFile(
            f"{part.pk}.jpg", _photo_bytes(label, color), content_type="image/jpeg"
        )
        return add_image(part.images, image=upload, caption="", by=user)
