"""Stage 14: public photos are explicit, re-encoded and served only while published."""

import importlib
from io import BytesIO

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.catalog.models import PartTypeImage, PublicPartPhoto, PublicPartPhotoRendition
from apps.catalog.public_photos import (
    CARD_EDGE,
    DETAIL_EDGE,
    MAX_PUBLISHED_PER_PART,
    PublicPhotoError,
    build_renditions,
    primary_photos,
    publish_photo,
    reject_photo,
    set_public_primary,
)
from tests.public_catalog_support import (
    assert_no_writes,
    capture,
    jpeg_bytes,
)


def _photo_url(photo, variant="card"):
    return f"/photos/{photo.public_id}/{variant}.jpg"


# --- Renditions ------------------------------------------------------------------------


def test_renditions_are_bounded_jpegs_without_metadata():
    exif = Image.Exif()
    exif[0x010F] = "Secret Camera Maker"  # Make
    exif[0x0110] = "Secret Model"  # Model
    exif[0x9003] = "2026:09:11 08:00:00"  # DateTimeOriginal
    source = jpeg_bytes(size=(4000, 3000), exif=exif.tobytes())

    renditions = {r.variant: r for r in build_renditions(BytesIO(source))}

    assert set(renditions) == {"card", "detail"}
    assert max(renditions["card"].width, renditions["card"].height) == CARD_EDGE
    assert max(renditions["detail"].width, renditions["detail"].height) == DETAIL_EDGE
    for rendition in renditions.values():
        decoded = Image.open(BytesIO(rendition.data))
        assert decoded.format == "JPEG"
        assert not decoded.getexif(), "EXIF must not survive re-encoding"
        assert b"Secret Camera Maker" not in rendition.data
        assert len(rendition.data) < len(source)


def test_transparent_png_is_flattened_on_white():
    image = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    card = build_renditions(BytesIO(buffer.getvalue()))[0]
    assert Image.open(BytesIO(card.data)).convert("RGB").getpixel((10, 10)) == (255, 255, 255)


@pytest.mark.parametrize(
    "payload",
    [
        b"not an image at all",
        b"\xff\xd8\xff" + b"\x00" * 64,  # JPEG magic, garbage body
        b"GIF89a" + b"\x00" * 64,
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    ],
)
def test_invalid_content_is_refused_safely(payload):
    with pytest.raises(PublicPhotoError):
        build_renditions(BytesIO(payload))


def test_decompression_bomb_is_refused():
    image = Image.new("1", (10_000, 10_000))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    with pytest.raises(PublicPhotoError):
        build_renditions(BytesIO(buffer.getvalue()))


# --- Moderation rules ----------------------------------------------------------------


def test_existing_uploads_are_not_public_until_a_person_publishes(public_catalog):
    part = public_catalog.part("Photo part", article="PH-1")
    public_catalog.image(part)

    assert primary_photos([part.pk]) == {}
    assert PublicPartPhoto.objects.count() == 0


def test_publish_records_provenance_and_the_first_photo_becomes_primary(public_catalog):
    part = public_catalog.part("Photo part", article="PH-2")
    first = publish_photo(
        public_catalog.image(part),
        source="manufacturer",
        note=" BRP  site ",
        by=public_catalog.user,
    )
    second = publish_photo(public_catalog.image(part), source="own", by=public_catalog.user)

    assert first.status == "published" and first.is_primary
    assert first.source == "manufacturer" and first.source_note == "BRP site"
    assert first.confirmed_by == public_catalog.user and first.confirmed_at is not None
    assert not second.is_primary
    assert first.renditions.count() == 2
    assert primary_photos([part.pk])[part.pk].public_id == first.public_id

    set_public_primary(second)
    assert primary_photos([part.pk])[part.pk].public_id == second.public_id


def test_publish_requires_a_known_source(public_catalog):
    image = public_catalog.image(public_catalog.part("Photo part", article="PH-3"))
    for source in ("", "internet", None):
        with pytest.raises(PublicPhotoError):
            publish_photo(image, source=source, by=public_catalog.user)
    assert not PublicPartPhoto.objects.exists()


def test_database_refuses_publication_without_provenance(public_catalog):
    image = public_catalog.image(public_catalog.part("Photo part", article="PH-4"))
    with pytest.raises(IntegrityError), transaction.atomic():
        PublicPartPhoto.objects.create(source_image=image, part=image.part, status="published")
    with pytest.raises(IntegrityError), transaction.atomic():
        PublicPartPhoto.objects.create(
            source_image=image, part=image.part, status="rejected", is_primary=True
        )


def test_only_one_published_primary_per_part(public_catalog):
    part = public_catalog.part("Photo part", article="PH-5")
    first = publish_photo(public_catalog.image(part), source="own", by=public_catalog.user)
    second = publish_photo(public_catalog.image(part), source="own", by=public_catalog.user)
    with pytest.raises(IntegrityError), transaction.atomic():
        PublicPartPhoto.objects.filter(pk=second.pk).update(is_primary=True)
    assert PublicPartPhoto.objects.get(pk=first.pk).is_primary


def test_reject_withdraws_and_promotes_the_next_primary(public_catalog):
    part = public_catalog.part("Photo part", article="PH-6")
    first_image = public_catalog.image(part)
    publish_photo(first_image, source="own", by=public_catalog.user)
    second = publish_photo(public_catalog.image(part), source="own", by=public_catalog.user)

    decision = reject_photo(first_image, by=public_catalog.user)

    assert decision.status == "rejected" and not decision.is_primary
    assert decision.rejected_by == public_catalog.user
    assert not decision.renditions.exists()
    assert PublicPartPhoto.objects.get(pk=second.pk).is_primary


def test_photo_count_per_part_is_capped(public_catalog):
    part = public_catalog.part("Photo part", article="PH-7")
    for _ in range(MAX_PUBLISHED_PER_PART):
        publish_photo(public_catalog.image(part), source="own", by=public_catalog.user)
    with pytest.raises(PublicPhotoError):
        publish_photo(public_catalog.image(part), source="own", by=public_catalog.user)


def test_deleted_internal_photo_cannot_be_published(public_catalog):
    image = public_catalog.image(public_catalog.part("Photo part", article="PH-8"))
    image.is_active = False
    image.save(update_fields=["is_active"])
    with pytest.raises(PublicPhotoError):
        publish_photo(image, source="own", by=public_catalog.user)


# --- Public serving --------------------------------------------------------------------


def test_published_photo_is_served_with_cache_and_etag(public_client, public_catalog):
    photo = publish_photo(
        public_catalog.image(public_catalog.part("Photo part", article="PH-9")),
        source="own",
        by=public_catalog.user,
    )

    response = public_client.get(_photo_url(photo, "detail"))

    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    assert response["Cache-Control"] == "public, max-age=86400"
    assert Image.open(BytesIO(response.content)).format == "JPEG"
    etag = response["ETag"]
    again = public_client.get(_photo_url(photo, "detail"), HTTP_IF_NONE_MATCH=etag)
    assert again.status_code == 304


def test_unpublished_rejected_hidden_and_guessed_photos_are_404(public_client, public_catalog):
    part = public_catalog.part("Photo part", article="PH-10")
    candidate = public_catalog.image(part)
    rejected_image = public_catalog.image(part)
    published = publish_photo(public_catalog.image(part), source="own", by=public_catalog.user)
    rejected = publish_photo(rejected_image, source="own", by=public_catalog.user)
    reject_photo(rejected_image, by=public_catalog.user)

    assert public_client.get(_photo_url(published)).status_code == 200
    for path in (
        _photo_url(rejected),
        f"/photos/{candidate.pk}/card.jpg",
        "/photos/00000000-0000-0000-0000-000000000000/card.jpg",
        f"/photos/{published.public_id}/original.jpg",
        f"/photos/{published.public_id}/../../media/x/card.jpg",
        "/photos/..%2F..%2Fetc%2Fpasswd/card.jpg",
        f"/media/{candidate.image.name}",
        f"/{candidate.image.name}",
        "/private_media/x.png",
    ):
        assert public_client.get(path).status_code == 404, path

    part.is_public = False
    part.save(update_fields=["is_public"])
    assert public_client.get(_photo_url(published)).status_code == 404


def test_unpublishing_takes_effect_immediately(public_client, public_catalog):
    image = public_catalog.image(public_catalog.part("Photo part", article="PH-11"))
    photo = publish_photo(image, source="own", by=public_catalog.user)
    assert public_client.get(_photo_url(photo)).status_code == 200
    reject_photo(image, by=public_catalog.user)
    assert public_client.get(_photo_url(photo)).status_code == 404


def test_result_card_and_detail_use_the_primary_photo_or_a_placeholder(
    public_client, public_catalog
):
    with_photo = public_catalog.part("Pictured gasket", article="PG-1")
    without = public_catalog.part("Plain gasket", article="PG-2")
    photo = publish_photo(public_catalog.image(with_photo), source="own", by=public_catalog.user)
    public_catalog.image(without)  # a candidate only: must not appear

    results = public_client.get("/search/", {"q": "gasket"}).content.decode()
    detail = public_client.get(f"/parts/{with_photo.public_id}/").content.decode()
    plain = public_client.get(f"/parts/{without.public_id}/").content.decode()

    assert f"/photos/{photo.public_id}/card.jpg?v={photo.version}" in results
    assert results.count("Фото появится после проверки") == 1
    assert f"/photos/{photo.public_id}/detail.jpg?v={photo.version}" in detail
    assert "/media/" not in results + detail + plain
    assert "Фото появится после проверки" in plain


# --- Internal moderation views --------------------------------------------------------


@pytest.fixture
def staff_client(public_catalog):
    client = Client()
    client.force_login(public_catalog.user)
    return client


def test_publish_view_requires_the_confirmation_and_a_source(staff_client, public_catalog):
    part = public_catalog.part("Moderated", article="MOD-1")
    image = public_catalog.image(part)
    url = f"/parts/images/{image.pk}/public/publish/"

    staff_client.post(url, {"source": "own"})
    assert not PublicPartPhoto.objects.exists()
    staff_client.post(url, {"confirm": "1"})
    assert not PublicPartPhoto.objects.exists()
    response = staff_client.post(url, {"source": "own", "confirm": "1"})

    assert response.status_code == 302
    assert PublicPartPhoto.objects.get().status == "published"
    detail = staff_client.get(f"/parts/{part.pk}/").content.decode()
    assert "Фото в публичном каталоге" in detail and "Опубликовано" in detail


def test_moderation_needs_the_catalog_permission(public_catalog, django_user_model):
    from django.contrib.auth.models import Group

    from apps.accounts import roles

    part = public_catalog.part("Moderated", article="MOD-2")
    image = public_catalog.image(part)
    storekeeper = django_user_model.objects.create_user(username="keeper", password="parol-12345")
    storekeeper.groups.add(Group.objects.get_or_create(name=roles.STOREKEEPER)[0])
    client = Client()
    client.force_login(storekeeper)

    response = client.post(
        f"/parts/images/{image.pk}/public/publish/", {"source": "own", "confirm": "1"}
    )
    assert response.status_code == 403
    assert client.get("/parts/public-photos/").status_code == 403
    assert not PublicPartPhoto.objects.exists()
    assert Client().get("/parts/public-photos/").status_code == 302


def test_queue_lists_candidates_published_and_rejected(staff_client, public_catalog):
    part = public_catalog.part("Queued part", article="Q-1")
    candidate = public_catalog.image(part)
    published_image = public_catalog.image(part)
    publish_photo(published_image, source="supplier", by=public_catalog.user)

    candidates = staff_client.get("/parts/public-photos/").content.decode()
    published = staff_client.get("/parts/public-photos/?state=published").content.decode()

    assert candidate.image.url in candidates and published_image.image.url not in candidates
    assert published_image.image.url in published and "Фото поставщика" in published


def test_deleting_the_internal_photo_withdraws_its_public_copy(staff_client, public_catalog):
    from tests.public_catalog_support import PUBLIC_HOST, public_runtime_settings

    image = public_catalog.image(public_catalog.part("Deleted photo part", article="DP-1"))
    photo = publish_photo(image, source="own", by=public_catalog.user)

    staff_client.post(f"/parts/images/{image.pk}/delete/")

    photo.refresh_from_db()
    assert photo.status == "rejected" and not photo.renditions.exists()
    with public_runtime_settings():
        assert Client(HTTP_HOST=PUBLIC_HOST).get(_photo_url(photo)).status_code == 404


def test_unconfirming_an_analog_keeps_the_link(staff_client, public_catalog):
    original = public_catalog.part("Original", article="O-1")
    analog = public_catalog.part("Analog", article="A-1")
    link = public_catalog.analog(original, analog)

    staff_client.post(f"/parts/analogs/{link.pk}/unconfirm/")

    link.refresh_from_db()
    assert not link.is_confirmed and link.confirmed_at is None and link.confirmed_by is None


# --- Migration -------------------------------------------------------------------------


def test_migration_creates_schema_only_and_never_backfills(public_catalog):
    migration = importlib.import_module("apps.catalog.migrations.0010_public_part_photos")
    kinds = {type(operation).__name__ for operation in migration.Migration.operations}
    assert kinds <= {"CreateModel", "AddIndex", "AddConstraint"}
    assert not PublicPartPhoto.objects.exists()
    assert PartTypeImage.objects.count() == 0


@pytest.mark.parametrize("count", [1, 20, 50])
def test_public_photo_reads_are_bounded(public_catalog, count, record_property):
    parts = [
        public_catalog.part(f"Bounded photo {index}", article=f"BPH-{index}")
        for index in range(count)
    ]
    for part in parts:
        publish_photo(public_catalog.image(part), source="own", by=public_catalog.user)
    with capture() as queries:
        photos = primary_photos([part.pk for part in parts])
    assert len(photos) == count
    record_property(f"public_primary_photos_queries_{count}", len(queries.captured_queries))
    assert len(queries.captured_queries) == 1
    assert_no_writes(queries)
    assert PublicPartPhotoRendition.objects.count() == 2 * count
    assert timezone.now()
