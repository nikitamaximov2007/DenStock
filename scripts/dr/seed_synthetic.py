"""Synthetic business data for the recovery drill (never production data).

Run inside a disposable web container: python manage.py shell < seed_synthetic.py
Creates parts with stock received through the real services, a customer, a
part photo in public media and files in private_media.
"""

from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile

from apps.catalog.models import Category, PartNumber, PartType, PartTypeImage, Unit
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import create_location

admin = get_user_model().objects.create_superuser("drill-owner", "", "not-used-drill-password")
supplier = Supplier.objects.create(name="Синтетический поставщик")
category = Category.objects.create(name="Синтетика")
unit = Unit.objects.get(name="Штука")
cell = create_location("S01-D01-C01", description="drill")
for n in range(12):
    part = PartType.objects.create(
        name=f"Сальник синтетический {25 + n}*62*7", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("150"),
        is_public=True,
    )
    PartNumber.objects.create(part=part, value=f"DRILL{n:04d}", kind=PartNumber.Kind.ARTICLE)
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(batch=batch, part_type=part, quantity=Decimal("5"),
                                    unit_cost_currency=Decimal("1"))
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    receive_stock_lot(create_stock_lot(line, cell, Decimal("5")), by=admin)
    if n == 0:
        image = PartTypeImage(part=part)
        image.image.save("drill-photo.jpg", ContentFile(b"\xff\xd8synthetic jpeg\xff\xd9"), save=True)
Customer.objects.create(name="Синтетический клиент", phone="+7 900 000-00-01")

private = Path(settings.PRIVATE_MEDIA_ROOT)
for rel, payload in (("customer_requests/1/drill-scan.pdf", b"%PDF synthetic scan"),
                     ("catalog-imports/drill-price.bin", bytes(range(256)) * 64)):
    target = private / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
print(f"SEEDED parts={PartType.objects.count()} customers={Customer.objects.count()}")
