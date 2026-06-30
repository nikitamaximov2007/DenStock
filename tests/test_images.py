"""Слой 24 — фотографии деталей и экземпляров.

Ключевой инвариант: фото — информационный слой. Может добавлять записи
`PartTypeImage`/`PartItemImage` и файлы в media, но НЕ создаёт `StockMovement`, не
меняет `StockBalance`/количества/статусы и не трогает scanner/barcode.
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartBarcode, PartType, Unit
from apps.core.files import validate_image_upload
from apps.core.scanner import resolve_scan
from apps.inventory.models import StockBalance, StockMovement
from apps.inventory.services import create_part_items, receive_part_item
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"

# Минимальные валидные сигнатуры (magic bytes) без Pillow.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64


def png(name="photo.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


def _finalized_line(sup, part, admin, *, qty):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("40"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(qty), unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Деталь-А", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL, min_price=Decimal("100"),
    )
    PartBarcode.objects.create(part=part, value="4600000000017")
    line = _finalized_line(sup, part, admin, qty="1")
    item = create_part_items(line, 1, serial_number="SN-1")[0]
    receive_part_item(item, to_location=loc, by=admin)
    return {"part": part, "item": item, "loc": loc}


def _login(client, make_user, role):
    make_user("u", role=role)
    client.login(username="u", password=PASSWORD)


# --- Загрузка / права --------------------------------------------------------


def test_storekeeper_can_upload_part_image(client, make_user, data):
    _login(client, make_user, roles.STOREKEEPER)
    resp = client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": png()})
    assert resp.status_code == 302
    assert data["part"].images.filter(is_active=True).count() == 1


def test_seller_cannot_upload_part_image(client, make_user, data):
    _login(client, make_user, roles.SELLER)
    resp = client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": png()})
    assert resp.status_code == 403
    assert data["part"].images.count() == 0


def test_storekeeper_can_upload_item_image(client, make_user, data):
    _login(client, make_user, roles.STOREKEEPER)
    resp = client.post(reverse("item_image_add", args=[data["item"].pk]), {"image": png()})
    assert resp.status_code == 302
    assert data["item"].images.filter(is_active=True).count() == 1


def test_seller_cannot_upload_item_image(client, make_user, data):
    _login(client, make_user, roles.SELLER)
    resp = client.post(reverse("item_image_add", args=[data["item"].pk]), {"image": png()})
    assert resp.status_code == 403


def test_anonymous_redirected_on_upload(client, data):
    resp = client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": png()})
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


# --- Primary логика ----------------------------------------------------------


def test_first_image_becomes_primary(client, make_user, data):
    _login(client, make_user, roles.MANAGER)
    client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": png("a.png")})
    img = data["part"].images.get()
    assert img.is_primary is True


def test_new_primary_resets_old(client, make_user, data):
    _login(client, make_user, roles.MANAGER)
    url = reverse("part_image_add", args=[data["part"].pk])
    client.post(url, {"image": png("a.png")})
    client.post(url, {"image": png("b.png")})
    first, second = list(data["part"].images.order_by("pk"))
    assert (first.is_primary, second.is_primary) == (True, False)
    client.post(reverse("part_image_primary", args=[second.pk]))
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.is_primary, second.is_primary) == (False, True)
    # Не более одного активного primary.
    assert data["part"].images.filter(is_primary=True, is_active=True).count() == 1


def test_delete_primary_promotes_next(client, make_user, data):
    _login(client, make_user, roles.MANAGER)
    url = reverse("part_image_add", args=[data["part"].pk])
    client.post(url, {"image": png("a.png")})
    client.post(url, {"image": png("b.png")})
    first, second = list(data["part"].images.order_by("pk"))
    client.post(reverse("part_image_delete", args=[first.pk]))
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.is_active is False
    assert second.is_primary is True


def test_soft_deleted_not_in_active_gallery(client, make_user, data):
    _login(client, make_user, roles.MANAGER)
    client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": png()})
    img = data["part"].images.get()
    client.post(reverse("part_image_delete", args=[img.pk]))
    assert data["part"].images.filter(is_active=True).count() == 0
    assert data["part"].images.count() == 1  # запись осталась (soft-delete)


# --- Валидация ---------------------------------------------------------------


@pytest.mark.parametrize(
    "content,name",
    [(PNG, "a.png"), (JPEG, "a.jpg"), (JPEG, "a.jpeg"), (WEBP, "a.webp")],
)
def test_valid_images_pass(content, name):
    validate_image_upload(SimpleUploadedFile(name, content))  # не бросает


@pytest.mark.parametrize("name", ["bad.svg", "bad.html", "bad.js", "bad.txt"])
def test_forbidden_extensions_rejected(name):
    with pytest.raises(ValidationError):
        validate_image_upload(SimpleUploadedFile(name, PNG))


def test_wrong_magic_bytes_rejected():
    # .png по имени, но внутри не PNG (фейковый content_type не спасает).
    fake = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
    with pytest.raises(ValidationError):
        validate_image_upload(fake)


def test_oversized_rejected():
    class _Big:
        name = "big.png"
        size = 11 * 1024 * 1024

        def read(self, *a):
            return PNG[:12]

        def seek(self, *a):
            return None

    with pytest.raises(ValidationError):
        validate_image_upload(_Big())


def test_upload_endpoint_rejects_svg(client, make_user, data):
    _login(client, make_user, roles.MANAGER)
    bad = SimpleUploadedFile("evil.svg", b"<svg/>", content_type="image/svg+xml")
    client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": bad})
    assert data["part"].images.count() == 0  # не сохранено


# --- Карточки показывают primary ---------------------------------------------


def test_part_card_shows_primary_image(client, make_user, data):
    _login(client, make_user, roles.MANAGER)
    client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": png()})
    html = client.get(reverse("part_detail", args=[data["part"].pk])).content.decode()
    assert "/media/part-types/" in html


def test_item_card_shows_primary_image(client, make_user, data):
    _login(client, make_user, roles.MANAGER)
    client.post(reverse("item_image_add", args=[data["item"].pk]), {"image": png()})
    html = client.get(reverse("item_detail", args=[data["item"].pk])).content.decode()
    assert "/media/part-items/" in html


# --- Read-only относительно склада -------------------------------------------


def test_upload_is_read_only_for_stock(client, make_user, data):
    _login(client, make_user, roles.MANAGER)
    item = data["item"]
    item.refresh_from_db()
    mv_before = StockMovement.objects.count()
    bal_before = sorted(StockBalance.objects.values_list("id", "quantity_available"))
    status_before = item.status
    barcode_before = item.internal_barcode
    client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": png("a.png")})
    client.post(reverse("item_image_add", args=[item.pk]), {"image": png("b.png")})
    assert StockMovement.objects.count() == mv_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_available")) == bal_before
    item.refresh_from_db()
    assert item.status == status_before
    assert item.internal_barcode == barcode_before
    # Сканер по-прежнему резолвит экземпляр по его коду.
    res = resolve_scan(item.internal_barcode)
    assert res.found and res.type == "part_item" and res.id == item.pk


# --- Просмотр / dev-serving / миграции ---------------------------------------


def test_guest_view_redirects_to_login(client, data):
    resp = client.get(reverse("part_detail", args=[data["part"].pk]))
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


def test_media_served_in_debug(client, make_user, data, settings):
    # static() добавляет media-маршрут только при DEBUG — перезагружаем urlconf и сбрасываем кэш.
    from importlib import reload

    from django.urls import clear_url_caches

    from config import urls as url_conf

    settings.DEBUG = True
    reload(url_conf)
    clear_url_caches()
    try:
        _login(client, make_user, roles.MANAGER)
        client.post(reverse("part_image_add", args=[data["part"].pk]), {"image": png()})
        img = data["part"].images.get()
        resp = client.get(img.image.url)
        assert resp.status_code == 200
    finally:
        settings.DEBUG = False
        reload(url_conf)
        clear_url_caches()


def test_no_pending_migrations(db):
    out = StringIO()
    try:
        call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
    except SystemExit:
        pytest.fail(f"Есть несозданные миграции:\n{out.getvalue()}")
