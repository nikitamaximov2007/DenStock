"""Shared fixtures for public catalog tests.

``public_client`` runs requests through the public runtime's own URLconf,
middleware, cookies and context processors (``apps.catalog.public_settings``),
not the internal stack, so what the tests prove is what catalog-web does.
"""

import copy
from decimal import Decimal
from io import BytesIO

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image

from apps.actions.models import PartCustomsInfo
from apps.catalog.models import (
    Category,
    Manufacturer,
    PartAnalog,
    PartNumber,
    PartType,
    Unit,
)
from apps.catalog.public_settings import PUBLIC_CONTEXT_PROCESSORS, PUBLIC_SETTINGS
from apps.core.images import add_image
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PUBLIC_HOST = "catalog.example"
WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "CREATE", "DROP", "TRUNCATE")


def public_runtime_settings(**extra):
    templates = copy.deepcopy(settings.TEMPLATES)
    templates[0]["OPTIONS"]["context_processors"] = list(PUBLIC_CONTEXT_PROCESSORS)
    values = {
        **PUBLIC_SETTINGS,
        "TEMPLATES": templates,
        "ALLOWED_HOSTS": [PUBLIC_HOST],
        "DEBUG": False,
        **extra,
    }
    return override_settings(**values)


@pytest.fixture
def public_client(db):
    with public_runtime_settings():
        yield Client(HTTP_HOST=PUBLIC_HOST)


def assert_no_writes(queries: CaptureQueriesContext) -> None:
    writes = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith(WRITE_PREFIXES)
    ]
    assert not writes, writes


def capture():
    return CaptureQueriesContext(connection)


def jpeg_bytes(size=(1600, 1200), color=(0, 104, 163), exif=None) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    if exif is not None:
        image.save(buffer, "JPEG", quality=90, exif=exif)
    else:
        image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class PublicCatalog:
    """Small builder for public catalog scenarios, using canonical services."""

    def __init__(self, user):
        self.user = user
        self.category = Category.objects.create(name="Public catalog tests")
        self.unit = Unit.objects.get(name="Штука")
        self.supplier = Supplier.objects.create(name="Public catalog supplier")
        self.location = StorageLocation.objects.create(
            name="Public test cell", code="S09-D01-C01", storage_allowed=True, is_active=True
        )

    def part(
        self,
        name,
        *,
        article=None,
        price="1000",
        maker="Canonical maker",
        russian=None,
        russian_confirmed=True,
        application="",
        unit=None,
        active=True,
        public=True,
    ) -> PartType:
        canonical_price = Decimal(price) if price is not None else None
        part = PartType.objects.create(
            name=name,
            category=self.category,
            manufacturer=Manufacturer.objects.get_or_create(name=maker)[0] if maker else None,
            unit=unit or self.unit,
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=canonical_price,
            certified_price_rub=canonical_price,
            price_provenance=(
                PartType.PriceProvenance.FORMULA_CERTIFIED
                if canonical_price is not None
                else PartType.PriceProvenance.UNVERIFIED
            ),
            is_active=active,
            is_public=public,
        )
        if article:
            PartNumber.objects.create(
                part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
            )
        if russian or application:
            PartCustomsInfo.objects.create(
                part_type=part,
                customs_name_ru=russian or "",
                customs_name_ru_confirmed=russian_confirmed,
                application_area=application,
            )
        return part

    def stock(self, part, quantity, *, location=None):
        batch = Batch.objects.create(supplier=self.supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch,
            part_type=part,
            quantity=Decimal(quantity),
            unit_cost_currency=Decimal("10"),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, self.user)
        line.refresh_from_db()
        lot = create_stock_lot(line, location or self.location, Decimal(quantity))
        receive_stock_lot(lot, by=self.user)
        return lot

    def analog(self, original, analog, *, confirmed=True) -> PartAnalog:
        link = PartAnalog.objects.create(original=original, analog=analog, created_by=self.user)
        if confirmed:
            from django.utils import timezone

            link.is_confirmed = True
            link.confirmed_at = timezone.now()
            link.confirmed_by = self.user
            link.save()
        return link

    def image(self, part, *, data=None, name="photo.jpg"):
        upload = SimpleUploadedFile(name, data or jpeg_bytes(), content_type="image/jpeg")
        return add_image(part.images, image=upload, caption="", by=self.user)


@pytest.fixture
def public_catalog(db, django_user_model, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    user = django_user_model.objects.create_superuser(
        username="public-catalog-admin", password="parol-12345"
    )
    return PublicCatalog(user)
