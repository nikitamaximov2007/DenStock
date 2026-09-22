from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.utils import timezone
from PIL import Image

from apps.catalog.models import Category, PartPhotoUploadAudit, PartType, Unit
from apps.catalog.photo_pipeline import PartPhotoAlreadyExists, upload_primary_part_photo
from apps.catalog.public_photos import primary_photos
from apps.customer_requests import operator_console
from apps.customer_requests.attachments import ValidatedAttachment
from apps.customer_requests.models import (
    CustomerRequest,
    OwnerPhotoUploadContext,
    OwnerPhotoUploadReceipt,
    StaffMessengerBinding,
)
from apps.sales.models import Sale


def _image(name="part.png"):
    output = BytesIO()
    Image.new("RGB", (32, 24), (30, 80, 140)).save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


@pytest.fixture
def part(db):
    category = Category.objects.create(name="Фото")
    unit, _created = Unit.objects.get_or_create(name="Фото шт", defaults={"short_name": "шт"})
    return PartType.objects.create(
        name="BALL BEARING", category=category, unit=unit
    )


def test_authoritative_upload_publishes_once_and_refuses_replacement(
    part, django_user_model, tmp_path, settings
):
    settings.MEDIA_ROOT = str(tmp_path)
    user = django_user_model.objects.create_superuser(username="photo-owner", password="x")

    result = upload_primary_part_photo(part=part, upload=_image(), source="desktop", by=user)

    assert result.image.is_primary is True
    assert PartPhotoUploadAudit.objects.get(image=result.image).source == "desktop"
    assert primary_photos([part.pk])[part.pk].public_id
    with pytest.raises(PartPhotoAlreadyExists):
        upload_primary_part_photo(
            part=part, upload=_image("second.png"), source="desktop", by=user
        )


def test_telegram_photo_flow_targets_selected_part_and_is_idempotent(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="denis", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user,
        operator_key="DENIS",
        provider="telegram",
        provider_user_id=7001,
        customer_visible_label="Денис",
    )
    sale = Sale.objects.create(
        number="S-PHOTO-1",
        status=Sale.Status.COMPLETED,
        customer_name="Иванов Иван Иванович",
        sold_at=timezone.now(),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])

    _text, panel = operator_console.owner_panel(binding)
    feed = operator_console.handle_callback(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        payload=panel["inline_keyboard"][2][0]["callback_data"],
    )
    assert feed[0].startswith("Продажи и ремонты")
    button_text = feed[1]["inline_keyboard"][0][0]["text"]
    assert "ПРОДАЖА" in button_text
    assert "Иванов Иван Иванович" in button_text
    assert timezone.localtime(sale.sold_at).strftime("%H:%M:%S") in button_text

    card = operator_console.handle_callback(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        payload=feed[1]["inline_keyboard"][0][0]["callback_data"],
    )
    assert card[1]["inline_keyboard"][0][0]["text"] == "Артикул не указан BALL BEARING"
    prompt = operator_console.handle_callback(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        payload=card[1]["inline_keyboard"][0][0]["callback_data"],
    )
    assert "Фото отсутствует" in prompt[0]
    context = OwnerPhotoUploadContext.objects.get(binding=binding)
    assert context.part_type_id == part.pk
    assert context.expires_at > timezone.now()

    image = _image().file.read()
    attachment = ValidatedAttachment(
        content=image, filename="photo.png", content_type="image/png"
    )
    result = operator_console.handle_text(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        external_id="update-1",
        text="",
        attachment=attachment,
    )
    assert result[0].startswith("Фото загружено")
    assert not OwnerPhotoUploadContext.objects.filter(binding=binding).exists()
    assert OwnerPhotoUploadReceipt.objects.filter(binding=binding, external_id="update-1").exists()
    assert CustomerRequest.objects.count() == 0

    duplicate = operator_console.handle_text(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        external_id="update-1",
        text="",
        attachment=attachment,
    )
    assert duplicate[0] == result[0]
    assert part.images.filter(is_active=True).count() == 1


@pytest.mark.postgresql
def test_pg16_concurrent_first_upload_and_context_selection_are_serialized(
    transactional_db, part, django_user_model, settings, tmp_path, monkeypatch
): 
    if connection.vendor != "postgresql":
        pytest.skip("requires the explicit PostgreSQL qualification database")
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="pg-photo-owner", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user,
        operator_key="DENIS",
        provider="telegram",
        provider_user_id=7101,
        customer_visible_label="Денис",
    )
    sale = Sale.objects.create(
        number="S-PG-PHOTO-1",
        status=Sale.Status.COMPLETED,
        customer_name="Петров Пётр Петрович",
        sold_at=timezone.now(),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])

    def select_target():
        close_old_connections()
        try:
            return operator_console._photo_selection(
                binding=binding, kind="sale", operation_id=sale.pk, part_id=part.pk
            )[0]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        selections = list(pool.map(lambda _item: select_target(), range(2)))
    assert all("Фото отсутствует" in text for text in selections)
    assert OwnerPhotoUploadContext.objects.filter(binding=binding).count() == 1

    def upload_once():
        close_old_connections()
        try:
            return upload_primary_part_photo(
                part=PartType.objects.get(pk=part.pk),
                upload=_image(),
                source="telegram",
                owner_operator_key="DENIS",
                operation_type="sale",
                operation_id=sale.pk,
            )
        except PartPhotoAlreadyExists:
            return None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        uploads = list(pool.map(lambda _item: upload_once(), range(2)))
    assert sum(item is not None for item in uploads) == 1
    assert part.images.filter(is_active=True).count() == 1
