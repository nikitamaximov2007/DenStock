"""The single authoritative PartType photo upload pipeline."""
from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.files import validate_image_upload

from .models import PartPhotoUploadAudit, PartType, PartTypeImage, PublicPartPhoto
from .public_photos import publish_photo, reject_photo


class PartPhotoAlreadyExists(ValueError):
    """The V1 flow never silently replaces an existing PartType photo."""


class PartPhotoTargetNotFound(ValueError):
    """The selected photo is no longer an active photo of the selected part."""


@dataclass(frozen=True, slots=True)
class PartPhotoUpload:
    image: PartTypeImage
    public_photo_id: int


@transaction.atomic
def upload_primary_part_photo(
    *,
    part: PartType,
    upload,
    source: str,
    by=None,
    owner_operator_key: str = "",
    operation_type: str = "",
    operation_id: int | None = None,
) -> PartPhotoUpload:
    """Create exactly one primary image and publish its safe renditions.

    The PartType row is the serialization point.  A second desktop request or
    Telegram worker therefore observes the first committed photo and fails
    closed instead of replacing it or creating a second primary.
    """
    if source not in PartPhotoUploadAudit.Source.values:
        raise ValueError("Неизвестный источник загрузки фото.")
    try:
        validate_image_upload(upload)
    except ValidationError:
        raise

    locked_part = PartType.objects.select_for_update().get(pk=part.pk)
    if (
        PartTypeImage.objects.filter(part_id=locked_part.pk, is_active=True).exists()
        or PublicPartPhoto.objects.filter(
            part_id=locked_part.pk, status=PublicPartPhoto.Status.PUBLISHED
        ).exists()
    ):
        raise PartPhotoAlreadyExists("Для этой детали фото уже было загружено.")

    image = None
    try:
        image = PartTypeImage.objects.create(
            part=locked_part,
            image=upload,
            caption="",
            is_primary=True,
            uploaded_by=by,
        )
        PartPhotoUploadAudit.objects.create(
            image=image,
            source=source,
            uploaded_by=by,
            owner_operator_key=(owner_operator_key or "")[:32],
            operation_type=(operation_type or "")[:12],
            operation_id=operation_id,
        )
        published = publish_photo(
            image,
            source=PublicPartPhoto.Source.OWN,
            note="Автоматическая публикация V1",
            by=by,
        )
    except Exception:
        if image is not None and image.image:
            image.image.delete(save=False)
        raise
    return PartPhotoUpload(image=image, public_photo_id=published.pk)


def _validate_photo_source(source: str) -> None:
    if source not in PartPhotoUploadAudit.Source.values:
        raise ValueError("Неизвестный источник загрузки фото.")


def _cleanup_uncommitted_image(image: PartTypeImage | None) -> None:
    if image is not None and image.image:
        image.image.delete(save=False)


@transaction.atomic
def upload_additional_part_photo(
    *,
    part: PartType,
    upload,
    source: str,
    by=None,
    owner_operator_key: str = "",
    operation_type: str = "",
    operation_id: int | None = None,
) -> PartPhotoUpload:
    """Append one validated photo to a part and publish its safe renditions.

    The PartType row serializes additions.  The first photo remains primary;
    later photos get an explicit trailing ``sort_order`` and never replace it.
    """
    _validate_photo_source(source)
    validate_image_upload(upload)
    locked_part = PartType.objects.select_for_update().get(pk=part.pk)
    active_images = list(
        PartTypeImage.objects.select_for_update()
        .filter(part_id=locked_part.pk, is_active=True)
        .order_by("sort_order", "uploaded_at", "pk")
    )
    next_sort_order = max((image.sort_order for image in active_images), default=-1) + 1
    image = None
    try:
        image = PartTypeImage.objects.create(
            part=locked_part,
            image=upload,
            caption="",
            is_primary=not active_images,
            sort_order=next_sort_order,
            uploaded_by=by,
        )
        PartPhotoUploadAudit.objects.create(
            image=image,
            source=source,
            uploaded_by=by,
            owner_operator_key=(owner_operator_key or "")[:32],
            operation_type=(operation_type or "")[:12],
            operation_id=operation_id,
        )
        published = publish_photo(
            image,
            source=PublicPartPhoto.Source.OWN,
            note="Автоматическая публикация V1",
            by=by,
        )
    except Exception:
        _cleanup_uncommitted_image(image)
        raise
    return PartPhotoUpload(image=image, public_photo_id=published.pk)


@transaction.atomic
def replace_part_photo(
    *,
    part: PartType,
    target_image_id: int,
    upload,
    source: str,
    by=None,
    owner_operator_key: str = "",
    operation_type: str = "",
    operation_id: int | None = None,
) -> PartPhotoUpload:
    """Atomically replace one active image while preserving its position.

    The old image is kept as an inactive historical row until the new image,
    audit row, public decision and renditions have all succeeded.  A failed
    upload therefore rolls back the database and removes only the new file.
    """
    _validate_photo_source(source)
    validate_image_upload(upload)
    locked_part = PartType.objects.select_for_update().get(pk=part.pk)
    active_images = list(
        PartTypeImage.objects.select_for_update()
        .filter(part_id=locked_part.pk, is_active=True)
        .order_by("sort_order", "uploaded_at", "pk")
    )
    target = next((image for image in active_images if image.pk == target_image_id), None)
    if target is None:
        raise PartPhotoTargetNotFound("Выбранное фото больше недоступно.")

    target_position = active_images.index(target)
    target_was_primary = target.is_primary
    target_public = (
        PublicPartPhoto.objects.select_for_update()
        .filter(source_image_id=target.pk)
        .first()
    )
    target_was_public_primary = bool(
        target_public and target_public.status == PublicPartPhoto.Status.PUBLISHED
        and target_public.is_primary
    )

    # Free the active-primary constraint before creating the replacement.  The
    # surrounding transaction restores it if any later step fails.
    if target_was_primary:
        target.is_primary = False
        target.save(update_fields=["is_primary"])

    image = None
    try:
        image = PartTypeImage.objects.create(
            part=locked_part,
            image=upload,
            caption=target.caption,
            is_primary=target_was_primary,
            sort_order=target.sort_order,
            uploaded_by=by,
        )
        PartPhotoUploadAudit.objects.create(
            image=image,
            source=source,
            uploaded_by=by,
            owner_operator_key=(owner_operator_key or "")[:32],
            operation_type=(operation_type or "")[:12],
            operation_id=operation_id,
        )

        # Normalize the pre-existing order once so the replacement occupies
        # the exact logical slot even for historical rows whose sort_order is
        # still tied at zero.
        ordered_after = [image if item.pk == target.pk else item for item in active_images]
        for position, item in enumerate(ordered_after):
            PartTypeImage.objects.filter(pk=item.pk).update(sort_order=position)
            PublicPartPhoto.objects.filter(source_image_id=item.pk).update(sort_order=position)
        image.sort_order = target_position
        image.save(update_fields=["sort_order"])

        published = publish_photo(
            image,
            source=PublicPartPhoto.Source.OWN,
            note="Автоматическая публикация V1",
            by=by,
        )
        if target_was_public_primary:
            PublicPartPhoto.objects.filter(
                part_id=locked_part.pk, is_primary=True
            ).exclude(pk=published.pk).update(is_primary=False)
            published.is_primary = True
            published.save(update_fields=["is_primary", "updated_at"])
        if target_public and target_public.status == PublicPartPhoto.Status.PUBLISHED:
            reject_photo(target, by=by)

        target.is_active = False
        target.is_primary = False
        target.save(update_fields=["is_active", "is_primary"])
    except Exception:
        _cleanup_uncommitted_image(image)
        raise
    return PartPhotoUpload(image=image, public_photo_id=published.pk)
