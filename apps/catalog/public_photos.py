"""Public catalog photos: explicit publication of internal part photos.

An internal ``PartTypeImage`` is only a candidate. It reaches the public
catalog when a catalog manager publishes that exact image and states where
it came from. Publication re-encodes the file with Pillow into two bounded
JPEG renditions stored in the database:

* re-encoding drops EXIF (camera, GPS) and any payload hidden in the file;
* the public runtime reads renditions through its restricted database role
  and never needs ``MEDIA_ROOT`` mounted;
* a card rendition keeps result pages light, the detail rendition caps the
  largest public image at ``DETAIL_EDGE`` pixels.

Historical uploads have no recorded provenance, so nothing here backfills
them: they stay candidates until a person decides.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import PartType, PartTypeImage, PublicPartPhoto, PublicPartPhotoRendition

MAX_PUBLISHED_PER_PART = 8
# Source uploads are already capped at 10 MB. A pixel cap refuses a small
# file that decodes into a huge bitmap before Pillow allocates it.
MAX_SOURCE_PIXELS = 40_000_000
CARD_EDGE = 480
DETAIL_EDGE = 1200
MAX_RENDITION_BYTES = 900 * 1024
JPEG = "image/jpeg"
_VARIANTS = (
    (PublicPartPhotoRendition.Variant.CARD, CARD_EDGE, 78),
    (PublicPartPhotoRendition.Variant.DETAIL, DETAIL_EDGE, 82),
)
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class PublicPhotoError(ValueError):
    """An operator-facing reason why a photo cannot be published."""


@dataclass(frozen=True, slots=True)
class PublicPhotoRef:
    """What a public page needs to link one published photo."""

    public_id: UUID
    version: str


@dataclass(frozen=True, slots=True)
class _Rendition:
    variant: str
    data: bytes
    width: int
    height: int
    sha256: str


# --- Public reads -------------------------------------------------------------------


def _published():
    return PublicPartPhoto.objects.filter(
        status=PublicPartPhoto.Status.PUBLISHED,
        part__is_public=True,
        part__is_active=True,
    )


def primary_photos(part_ids: Iterable[int]) -> dict[int, PublicPhotoRef]:
    """The first published photo of each part, in one query."""
    ids = list(dict.fromkeys(part_ids))
    if not ids:
        return {}
    rows = (
        _published()
        .filter(part_id__in=ids)
        .order_by("part_id", "-is_primary", "sort_order", "pk")
        .values_list("part_id", "public_id", "version")
    )
    photos: dict[int, PublicPhotoRef] = {}
    for part_id, public_id, version in rows:
        photos.setdefault(part_id, PublicPhotoRef(public_id=public_id, version=version))
    return photos


def part_photos(part_id: int) -> list[PublicPhotoRef]:
    """Every published photo of one part, primary first."""
    rows = (
        _published()
        .filter(part_id=part_id)
        .order_by("-is_primary", "sort_order", "pk")
        .values_list("public_id", "version")[:MAX_PUBLISHED_PER_PART]
    )
    return [PublicPhotoRef(public_id=public_id, version=version) for public_id, version in rows]


def rendition_for(public_id: UUID, variant: str):
    """Bytes and headers of one rendition, or None unless it is public now."""
    if variant not in PublicPartPhotoRendition.Variant.values:
        return None
    return (
        PublicPartPhotoRendition.objects.filter(
            photo__public_id=public_id,
            photo__status=PublicPartPhoto.Status.PUBLISHED,
            photo__part__is_public=True,
            photo__part__is_active=True,
            variant=variant,
        )
        .values("data", "content_type", "sha256", "byte_size")
        .first()
    )


# --- Renditions ---------------------------------------------------------------------


def _flatten(image: Image.Image) -> Image.Image:
    """RGB on white: JPEG has no alpha, and transparent PNGs must not go black."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _encode(image: Image.Image, quality: int) -> bytes:
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
    return buffer.getvalue()


def build_renditions(fileobj) -> list[_Rendition]:
    """Decode an upload once and produce the bounded public renditions."""
    try:
        with Image.open(fileobj) as source:
            if source.format not in _ALLOWED_FORMATS:
                raise PublicPhotoError("Публиковать можно только JPG, PNG или WEBP.")
            width, height = source.size
            if width < 1 or height < 1 or width * height > MAX_SOURCE_PIXELS:
                raise PublicPhotoError("Размер изображения вне допустимых пределов.")
            source.seek(0)
            source.load()
            upright = _flatten(ImageOps.exif_transpose(source))
    except PublicPhotoError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise PublicPhotoError("Файл не читается как изображение.") from None

    renditions = []
    for variant, edge, quality in _VARIANTS:
        copy = upright.copy()
        copy.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        data = _encode(copy, quality)
        if len(data) > MAX_RENDITION_BYTES:
            data = _encode(copy, 65)
        if len(data) > MAX_RENDITION_BYTES:
            raise PublicPhotoError("Фото слишком детальное для каталога: уменьшите его.")
        renditions.append(
            _Rendition(
                variant=variant,
                data=data,
                width=copy.width,
                height=copy.height,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return renditions


# --- Moderation (internal runtime only) -----------------------------------------------
#
# Every decision locks the part row first, so two managers acting on photos of
# the same part are serialized and the "one primary" rule never races.


def _lock_part(part_id: int) -> None:
    PartType.objects.select_for_update().filter(pk=part_id).values_list("pk", flat=True).first()


def _promote_next_primary(part_id: int) -> None:
    has_primary = PublicPartPhoto.objects.filter(
        part_id=part_id, status=PublicPartPhoto.Status.PUBLISHED, is_primary=True
    ).exists()
    if has_primary:
        return
    candidate = (
        PublicPartPhoto.objects.filter(part_id=part_id, status=PublicPartPhoto.Status.PUBLISHED)
        .order_by("sort_order", "pk")
        .first()
    )
    if candidate is not None:
        candidate.is_primary = True
        candidate.save(update_fields=["is_primary", "updated_at"])


@transaction.atomic
def publish_photo(image: PartTypeImage, *, source: str, note: str = "", by) -> PublicPartPhoto:
    """Publish one internal photo after a person confirmed it and its source."""
    if source not in PublicPartPhoto.Source.values:
        raise PublicPhotoError("Укажите, откуда фото.")
    note = " ".join((note or "").split())[:255]
    _lock_part(image.part_id)
    image = PartTypeImage.objects.select_for_update().select_related("part").get(pk=image.pk)
    if not image.is_active:
        raise PublicPhotoError("Это фото удалено из карточки детали.")
    decision = PublicPartPhoto.objects.filter(source_image=image).first()
    already_public = decision is not None and decision.status == PublicPartPhoto.Status.PUBLISHED
    if not already_public:
        published = PublicPartPhoto.objects.filter(
            part_id=image.part_id, status=PublicPartPhoto.Status.PUBLISHED
        ).count()
        if published >= MAX_PUBLISHED_PER_PART:
            raise PublicPhotoError(
                f"У детали уже {MAX_PUBLISHED_PER_PART} опубликованных фото. Снимите лишнее."
            )
    try:
        with image.image.open("rb") as handle:
            renditions = build_renditions(handle)
    except FileNotFoundError:
        raise PublicPhotoError("Файл фото не найден на сервере.") from None

    now = timezone.now()
    version = hashlib.sha256(
        "".join(rendition.sha256 for rendition in renditions).encode()
    ).hexdigest()[:16]
    if decision is None:
        decision = PublicPartPhoto(source_image=image, part_id=image.part_id)
    decision.part_id = image.part_id
    decision.status = PublicPartPhoto.Status.PUBLISHED
    decision.source = source
    decision.source_note = note
    decision.sort_order = image.sort_order
    decision.version = version
    decision.confirmed_at = now
    decision.confirmed_by = by
    decision.rejected_at = None
    decision.rejected_by = None
    decision.save()
    decision.renditions.all().delete()
    PublicPartPhotoRendition.objects.bulk_create(
        [
            PublicPartPhotoRendition(
                photo=decision,
                variant=rendition.variant,
                content_type=JPEG,
                data=rendition.data,
                width=rendition.width,
                height=rendition.height,
                byte_size=len(rendition.data),
                sha256=rendition.sha256,
            )
            for rendition in renditions
        ]
    )
    _promote_next_primary(image.part_id)
    decision.refresh_from_db()
    return decision


@transaction.atomic
def reject_photo(image: PartTypeImage, *, by) -> PublicPartPhoto:
    """Keep a photo out of the public catalog; also withdraws a published one."""
    _lock_part(image.part_id)
    image = PartTypeImage.objects.select_for_update().get(pk=image.pk)
    decision = PublicPartPhoto.objects.filter(source_image=image).first()
    if decision is None:
        decision = PublicPartPhoto(source_image=image, part_id=image.part_id)
    decision.status = PublicPartPhoto.Status.REJECTED
    decision.is_primary = False
    decision.version = ""
    decision.rejected_at = timezone.now()
    decision.rejected_by = by
    decision.save()
    decision.renditions.all().delete()
    _promote_next_primary(image.part_id)
    return decision


def withdraw_for_source(image: PartTypeImage, *, by) -> None:
    """The internal photo was deleted: its public copy must not outlive it."""
    if PublicPartPhoto.objects.filter(
        source_image=image, status=PublicPartPhoto.Status.PUBLISHED
    ).exists():
        reject_photo(image, by=by)


@transaction.atomic
def set_public_primary(photo: PublicPartPhoto) -> None:
    _lock_part(photo.part_id)
    photo = PublicPartPhoto.objects.select_for_update().get(pk=photo.pk)
    if photo.status != PublicPartPhoto.Status.PUBLISHED or photo.is_primary:
        return
    PublicPartPhoto.objects.filter(part_id=photo.part_id, is_primary=True).update(
        is_primary=False
    )
    photo.is_primary = True
    photo.save(update_fields=["is_primary", "updated_at"])


def moderation_rows(part) -> list[dict]:
    """Active internal photos of a part with their public decision, for the card."""
    images = list(part.images.filter(is_active=True).order_by("sort_order", "uploaded_at"))
    decisions = {
        decision.source_image_id: decision
        for decision in PublicPartPhoto.objects.filter(source_image__in=images)
    }
    return [{"image": image, "decision": decisions.get(image.pk)} for image in images]
