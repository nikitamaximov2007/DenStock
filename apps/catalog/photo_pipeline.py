"""The single authoritative PartType photo upload pipeline."""
from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.files import validate_image_upload

from .models import PartPhotoUploadAudit, PartType, PartTypeImage, PublicPartPhoto
from .public_photos import publish_photo


class PartPhotoAlreadyExists(ValueError):
    """The V1 flow never silently replaces an existing PartType photo."""


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
