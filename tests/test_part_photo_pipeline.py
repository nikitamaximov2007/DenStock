import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import override_settings
from django.utils import timezone
from PIL import Image

from apps.catalog.models import (
    Category,
    PartPhotoUploadAudit,
    PartType,
    PartTypeImage,
    PublicPartPhoto,
    Unit,
)
from apps.catalog.photo_pipeline import (
    PartPhotoAlreadyExists,
    replace_part_photo,
    upload_additional_part_photo,
    upload_primary_part_photo,
)
from apps.catalog.public_photos import PublicPhotoError, part_photos, primary_photos
from apps.core.time import format_perm_datetime
from apps.customer_requests import max_bot, operator_console
from apps.customer_requests.attachments import ValidatedAttachment
from apps.customer_requests.models import (
    CustomerRequest,
    OwnerPhotoUploadContext,
    OwnerPhotoUploadReceipt,
    StaffMessengerBinding,
)
from apps.sales.models import Sale

from .max_fake import message_callback, message_created


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
        sold_at=datetime(2026, 9, 23, 11, 59, 35, tzinfo=UTC),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])

    _text, panel = operator_console.owner_panel(binding)
    assert panel["keyboard"] == [
        [{"text": "Все заявки"}],
        [{"text": "Новые заявки"}],
        [{"text": "Загрузка фото по продажам/ремонтам"}],
    ]
    feed = operator_console.handle_text(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        external_id="photo-menu",
        text="Загрузка фото по продажам/ремонтам",
    )
    assert feed[0].startswith("Продажи и ремонты")
    button_text = feed[1]["inline_keyboard"][0][0]["text"]
    assert "ПРОДАЖА" in button_text
    assert "Иванов Иван Иванович" in button_text
    assert format_perm_datetime(sale.sold_at) == "23.09.2026 16:59:35"
    assert format_perm_datetime(sale.sold_at) in button_text

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
    assert result[0].startswith("Фото добавлено")
    assert OwnerPhotoUploadContext.objects.get(binding=binding).mode == (
        OwnerPhotoUploadContext.Mode.MANAGE
    )
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


def test_max_photo_flow_uses_native_buttons_and_shared_photo_service(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="max-denis", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user,
        operator_key="DENIS",
        provider="max",
        provider_user_id=7401,
        delivery_chat_id=8401,
        customer_visible_label="Денис",
    )
    sale = Sale.objects.create(
        number="S-MAX-PHOTO-1",
        status=Sale.Status.COMPLETED,
        customer_name="Иванов Иван Иванович",
        sold_at=datetime(2026, 9, 23, 11, 59, 35, tzinfo=UTC),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])

    _text, panel = operator_console.owner_panel(binding)
    max_panel = operator_console.buttons_for_provider(panel, "max")
    assert [row[0]["text"] for row in max_panel["inline_keyboard"]] == [
        "Все заявки",
        "Новые заявки",
        "Загрузка фото по продажам/ремонтам",
    ]
    assert all(
        "callback_data" not in button
        for row in max_panel["inline_keyboard"]
        for button in row
    )

    feed = operator_console.handle_callback(
        provider="max",
        provider_user_id=binding.provider_user_id,
        payload=max_panel["inline_keyboard"][2][0]["payload"],
    )
    assert "ПРОДАЖА" in feed[1]["inline_keyboard"][0][0]["text"]
    assert "Иванов Иван Иванович" in feed[1]["inline_keyboard"][0][0]["text"]
    assert format_perm_datetime(sale.sold_at) in feed[1]["inline_keyboard"][0][0]["text"]

    max_feed = operator_console.buttons_for_provider(feed[1], "max")
    card = operator_console.handle_callback(
        provider="max",
        provider_user_id=binding.provider_user_id,
        payload=max_feed["inline_keyboard"][0][0]["payload"],
    )
    assert card[1]["inline_keyboard"][0][0]["text"] == "Артикул не указан BALL BEARING"
    max_card = operator_console.buttons_for_provider(card[1], "max")
    prompt = operator_console.handle_callback(
        provider="max",
        provider_user_id=binding.provider_user_id,
        payload=max_card["inline_keyboard"][0][0]["payload"],
    )
    assert "Фото отсутствует" in prompt[0]
    context = OwnerPhotoUploadContext.objects.get(binding=binding)
    assert context.part_type_id == part.pk

    result = operator_console.handle_text(
        provider="max",
        provider_user_id=binding.provider_user_id,
        external_id="max-photo-1",
        text="",
        provider_chat_id=binding.delivery_chat_id,
        attachment=ValidatedAttachment(
            content=_image().file.read(), filename="max-photo.png", content_type="image/png"
        ),
    )
    assert result[0].startswith("Фото добавлено")
    assert PartPhotoUploadAudit.objects.get(image__part=part).source == "max"
    assert PublicPartPhoto.objects.filter(part=part, status="published").exists()
    assert OwnerPhotoUploadContext.objects.get(binding=binding).mode == (
        OwnerPhotoUploadContext.Mode.MANAGE
    )
    duplicate = operator_console.handle_text(
        provider="max",
        provider_user_id=binding.provider_user_id,
        external_id="max-photo-1",
        text="",
        provider_chat_id=binding.delivery_chat_id,
        attachment=ValidatedAttachment(
            content=_image().file.read(), filename="max-photo.png", content_type="image/png"
        ),
    )
    assert duplicate[0] == result[0]


@override_settings(TIME_ZONE="UTC")
def test_photo_feed_uses_explicit_perm_timezone_and_authoritative_sorting(
    db, django_user_model, settings
):
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="perm-time", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user,
        operator_key="DENIS",
        provider="telegram",
        provider_user_id=7402,
        customer_visible_label="Денис",
    )
    older = Sale.objects.create(
        number="S-PERM-OLD",
        status=Sale.Status.COMPLETED,
        customer_name="Старая операция",
        sold_at=datetime(2026, 9, 23, 10, 0, tzinfo=UTC),
    )
    newer = Sale.objects.create(
        number="S-PERM-NEW",
        status=Sale.Status.COMPLETED,
        customer_name="Новая операция",
        sold_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )

    expected_new = "23.09.2026 17:00:00"
    expected_old = "23.09.2026 15:00:00"
    assert format_perm_datetime(newer.sold_at) == expected_new
    assert format_perm_datetime(older.sold_at) == expected_old
    with override_settings(TIME_ZONE="Europe/Moscow"):
        assert format_perm_datetime(newer.sold_at) == expected_new

    _text, markup = operator_console.photo_operation_page(binding=binding)
    operation_labels = [row[0]["text"] for row in markup["inline_keyboard"][:2]]
    assert operation_labels == [
        f"{expected_new} ПРОДАЖА\nНовая операция",
        f"{expected_old} ПРОДАЖА\nСтарая операция",
    ]

    card_text, _card_markup = operator_console.photo_operation_card(
        binding=binding, kind="sale", operation_id=newer.pk
    )
    assert card_text.startswith(f"ПРОДАЖА {expected_new}")


def test_max_webhook_photo_path_preserves_native_payload_and_rejects_unbound_owner(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="max-rim", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user,
        operator_key="RIM",
        provider="max",
        provider_user_id=7501,
        delivery_chat_id=8501,
        customer_visible_label="Рим",
    )
    Sale.objects.create(
        number="S-MAX-PHOTO-2",
        status=Sale.Status.COMPLETED,
        customer_name="Петров Пётр Петрович",
        sold_at=timezone.now(),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])
    _text, panel = operator_console.owner_panel(binding)
    max_panel = operator_console.buttons_for_provider(panel, "max")

    assert max_bot.handle_update(
        message_callback(binding.provider_user_id, binding.delivery_chat_id,
                         max_panel["inline_keyboard"][2][0]["payload"])
    ) == "operator"
    feed = operator_console.handle_callback(
        provider="max", provider_user_id=binding.provider_user_id,
        payload=max_panel["inline_keyboard"][2][0]["payload"],
    )
    operation_payload = operator_console.buttons_for_provider(feed[1], "max")[
        "inline_keyboard"
    ][0][0]["payload"]
    card = operator_console.handle_callback(
        provider="max", provider_user_id=binding.provider_user_id, payload=operation_payload
    )
    part_payload = operator_console.buttons_for_provider(card[1], "max")[
        "inline_keyboard"
    ][0][0]["payload"]
    assert max_bot.handle_update(
        message_callback(binding.provider_user_id, binding.delivery_chat_id, part_payload)
    ) == "operator"

    image = _image("from-max.png").file.read()
    update = message_created(
        binding.provider_user_id, binding.delivery_chat_id, "", mid="max-photo-mid"
    )
    update["message"]["body"]["attachments"] = [{
        "type": "image",
        "payload": {
            "content_base64": base64.b64encode(image).decode("ascii"),
        },
    }]
    assert max_bot.handle_update(
        update,
        attachment_loader=lambda body: max_bot.load_operator_attachment(_InlineMaxApi(), body),
    ) == "operator"
    assert PartPhotoUploadAudit.objects.get(image__part=part).source == "max"

    denied = operator_console.handle_callback(
        provider="max", provider_user_id=999991, payload=part_payload
    )
    assert denied == ("Недоступно.", None)


class _InlineMaxApi:
    def download_url(self, url):
        raise AssertionError("MAX inline base64 attachment must not call a download URL")


def test_telegram_and_max_photo_contexts_are_isolated_by_binding(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    unit = Unit.objects.get(name="Фото шт")
    second_part = PartType.objects.create(name="OIL FILTER", category=part.category, unit=unit)
    user = django_user_model.objects.create_superuser(username="shared-owner", password="x")
    telegram = StaffMessengerBinding.objects.create(
        user=user, operator_key="DENIS", provider="telegram", provider_user_id=7601,
        customer_visible_label="Денис",
    )
    max_binding = StaffMessengerBinding.objects.create(
        user=user, operator_key="DENIS", provider="max", provider_user_id=7602,
        delivery_chat_id=8602, customer_visible_label="Денис",
    )
    sale_a = Sale.objects.create(
        number="S-ISOLATION-A", status=Sale.Status.COMPLETED,
        customer_name="Первый Клиент", sold_at=timezone.now(),
    )
    sale_b = Sale.objects.create(
        number="S-ISOLATION-B", status=Sale.Status.COMPLETED,
        customer_name="Второй Клиент", sold_at=timezone.now(),
    )
    monkeypatch.setattr(
        operator_console,
        "_photo_operation_lines",
        lambda operation, kind: {sale_a.pk: [part], sale_b.pk: [second_part]}[operation.pk],
    )

    assert "Фото отсутствует" in operator_console._photo_selection(
        binding=telegram, kind="sale", operation_id=sale_a.pk, part_id=part.pk
    )[0]
    assert "Фото отсутствует" in operator_console._photo_selection(
        binding=max_binding, kind="sale", operation_id=sale_b.pk, part_id=second_part.pk
    )[0]
    contexts = OwnerPhotoUploadContext.objects.in_bulk(field_name="binding_id")
    assert contexts[telegram.pk].part_type_id == part.pk
    assert contexts[max_binding.pk].part_type_id == second_part.pk
    StaffMessengerBinding.objects.filter(pk__in=[telegram.pk, max_binding.pk]).update(
        operator_mode=True
    )

    telegram_result = operator_console.handle_text(
        provider="telegram", provider_user_id=telegram.provider_user_id,
        external_id="tg-isolation", text="", attachment=ValidatedAttachment(
            content=_image("telegram.png").file.read(), filename="telegram.png",
            content_type="image/png",
        ),
    )
    max_result = operator_console.handle_text(
        provider="max", provider_user_id=max_binding.provider_user_id,
        external_id="max-isolation", text="", attachment=ValidatedAttachment(
            content=_image("max.png").file.read(), filename="max.png",
            content_type="image/png",
        ),
    )
    assert telegram_result[0].startswith("Фото добавлено")
    assert max_result[0].startswith("Фото добавлено")
    assert PartPhotoUploadAudit.objects.get(image__part=part).source == "telegram"
    assert PartPhotoUploadAudit.objects.get(image__part=second_part).source == "max"


def test_expired_or_revoked_max_photo_context_fails_closed(
    part, django_user_model, settings, tmp_path
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="revoked-max", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user, operator_key="RIM", provider="max", provider_user_id=7701,
        delivery_chat_id=8701, customer_visible_label="Рим",
    )
    binding.operator_mode = True
    binding.save(update_fields=["operator_mode", "updated_at"])
    OwnerPhotoUploadContext.objects.create(
        binding=binding, part_type=part, operation_type="sale", operation_id=1,
        article_snapshot="420832176", part_name_snapshot=part.name,
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    expired = operator_console.handle_text(
        provider="max", provider_user_id=binding.provider_user_id,
        external_id="expired-max", text="", attachment=ValidatedAttachment(
            content=_image().file.read(), filename="expired.png", content_type="image/png",
        ),
    )
    assert expired[0] == (
        "Срок выбора детали истёк. Сначала выберите деталь в разделе загрузки фото."
    )
    binding.is_active = False
    binding.save(update_fields=["is_active", "updated_at"])
    _text, panel = operator_console.owner_panel(binding)
    assert operator_console.handle_callback(
        provider="max", provider_user_id=binding.provider_user_id,
        payload=panel["inline_keyboard"][2][0]["callback_data"],
    ) == ("Недоступно.", None)


def _photo_attachment(name="photo.png", content=None):
    upload = _image(name)
    return ValidatedAttachment(
        content=content if content is not None else upload.file.read(),
        filename=name,
        content_type="image/png",
    )


def test_telegram_photo_session_adds_three_photos_and_done_clears_state(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="multi-owner", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user, operator_key="DENIS", provider="telegram", provider_user_id=7801,
        customer_visible_label="Денис", operator_mode=True,
    )
    sale = Sale.objects.create(
        number="S-MULTI-PHOTO", status=Sale.Status.COMPLETED,
        customer_name="Тестовый клиент", sold_at=timezone.now(),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])

    prompt = operator_console._photo_selection(
        binding=binding, kind="sale", operation_id=sale.pk, part_id=part.pk
    )
    assert "Фото отсутствует" in prompt[0]

    def upload(external_id, filename):
        return operator_console.handle_text(
            provider="telegram", provider_user_id=binding.provider_user_id,
            external_id=external_id, text="", attachment=_photo_attachment(filename),
        )

    first = upload("multi-1", "one.png")
    assert first[0].startswith("Фото добавлено")
    assert [row[0]["text"] for row in first[1]["inline_keyboard"]] == [
        "Добавить ещё фото", "Заменить фото", "Готово"
    ]
    duplicate = upload("multi-1", "one.png")
    assert duplicate[0] == first[0]
    assert part.images.filter(is_active=True).count() == 1

    for external_id, filename in (("multi-2", "two.png"), ("multi-3", "three.png")):
        add = operator_console.handle_callback(
            provider="telegram", provider_user_id=binding.provider_user_id,
            payload=first[1]["inline_keyboard"][0][0]["callback_data"],
        )
        assert "ещё одно фото" in add[0]
        first = upload(external_id, filename)

    images = list(part.images.filter(is_active=True).order_by("sort_order", "uploaded_at", "pk"))
    assert len(images) == 3
    assert all(image.image.name.endswith(".png") for image in images)
    assert [image.is_primary for image in images] == [True, False, False]
    assert (
        PublicPartPhoto.objects.filter(
            part=part, status=PublicPartPhoto.Status.PUBLISHED
        ).count()
        == 3
    )
    assert len(part_photos(part.pk)) == 3
    assert primary_photos([part.pk])[part.pk].public_id == (
        PublicPartPhoto.objects.get(source_image=images[0]).public_id
    )

    done = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=first[1]["inline_keyboard"][2][0]["callback_data"],
    )
    assert done[0] == "Сеанс работы с фото завершён."
    assert not OwnerPhotoUploadContext.objects.filter(binding=binding).exists()
    unrelated = operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="after-done", text="", attachment=_photo_attachment("four.png"),
    )
    assert unrelated[0] == "Сначала выберите заявку в рабочей панели."
    assert part.images.filter(is_active=True).count() == 3


def test_multiple_photo_replace_targets_only_selected_image_and_preserves_order(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="replace-owner", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user, operator_key="RIM", provider="telegram", provider_user_id=7802,
        customer_visible_label="Рим", operator_mode=True,
    )
    first = upload_primary_part_photo(
        part=part, upload=_image("one.png"), source="telegram", by=user
    ).image
    second = upload_additional_part_photo(
        part=part, upload=_image("two.png"), source="telegram", by=user
    ).image
    third = upload_additional_part_photo(
        part=part, upload=_image("three.png"), source="telegram", by=user
    ).image
    sale = Sale.objects.create(
        number="S-REPLACE-MULTI", status=Sale.Status.COMPLETED,
        customer_name="Тестовый клиент", sold_at=timezone.now(),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])

    manage = operator_console._photo_selection(
        binding=binding, kind="sale", operation_id=sale.pk, part_id=part.pk
    )
    replace = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=manage[1]["inline_keyboard"][1][0]["callback_data"],
    )
    assert replace[0] == "Выберите фото, которое хотите заменить."
    choices = replace[1]["inline_keyboard"]
    assert [row[0]["text"] for row in choices[:3]] == [
        "Фото 1 (главное)", "Фото 2", "Фото 3"
    ]
    second_choice = choices[1][0]["callback_data"]
    target_token = second_choice.split(":")[-1]
    assert len(target_token) == 16
    assert all(character in "0123456789abcdef" for character in target_token)
    assert target_token != str(second.pk)

    upload_prompt = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=second_choice,
    )
    assert upload_prompt[0] == "Пришлите новое фото."
    result = operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="replace-2", text="", attachment=_photo_attachment("replacement.png"),
    )
    assert result[0].startswith("Фото добавлено")

    first.refresh_from_db()
    second.refresh_from_db()
    third.refresh_from_db()
    active = list(part.images.filter(is_active=True).order_by("sort_order", "uploaded_at", "pk"))
    replacement = active[1]
    assert active == [first, replacement, third]
    assert [image.is_primary for image in active] == [True, False, False]
    assert second.is_active is False
    assert (
        PublicPartPhoto.objects.get(source_image=second).status
        == PublicPartPhoto.Status.REJECTED
    )
    assert (
        PublicPartPhoto.objects.get(source_image=replacement).status
        == PublicPartPhoto.Status.PUBLISHED
    )
    assert len(part_photos(part.pk)) == 3
    assert primary_photos([part.pk])[part.pk].public_id == (
        PublicPartPhoto.objects.get(source_image=first).public_id
    )


def test_single_photo_failed_replacement_keeps_old_photo_and_retry_is_safe(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="replace-one", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user, operator_key="DENIS", provider="telegram", provider_user_id=7803,
        customer_visible_label="Денис", operator_mode=True,
    )
    original = upload_primary_part_photo(
        part=part, upload=_image("original.png"), source="telegram", by=user
    ).image
    sale = Sale.objects.create(
        number="S-REPLACE-ONE", status=Sale.Status.COMPLETED,
        customer_name="Тестовый клиент", sold_at=timezone.now(),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])
    manage = operator_console._photo_selection(
        binding=binding, kind="sale", operation_id=sale.pk, part_id=part.pk
    )
    confirm = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=manage[1]["inline_keyboard"][1][0]["callback_data"],
    )
    ready = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=confirm[1]["inline_keyboard"][0][0]["callback_data"],
    )
    assert ready[0] == "Пришлите новое фото."
    failed = operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="replace-invalid", text="", attachment=_photo_attachment(
            "invalid.png", content=b"not an image"
        ),
    )
    assert "изображение" in failed[0].lower()
    original.refresh_from_db()
    assert original.is_active is True
    assert part.images.filter(is_active=True).count() == 1
    assert OwnerPhotoUploadContext.objects.get(binding=binding).mode == (
        OwnerPhotoUploadContext.Mode.REPLACE_UPLOAD
    )

    canceled = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=failed[1]["inline_keyboard"][0][0]["callback_data"],
    )
    assert canceled[0] == "Загрузка фото отменена."
    assert part.images.filter(is_active=True).count() == 1

    manage = operator_console._photo_selection(
        binding=binding, kind="sale", operation_id=sale.pk, part_id=part.pk
    )
    confirm = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=manage[1]["inline_keyboard"][1][0]["callback_data"],
    )
    ready = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=confirm[1]["inline_keyboard"][0][0]["callback_data"],
    )
    succeeded = operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="replace-valid", text="", attachment=_photo_attachment("new.png"),
    )
    assert succeeded[0].startswith("Фото добавлено")
    original.refresh_from_db()
    assert original.is_active is False
    replacement = PartTypeImage.objects.get(part=part, is_active=True)
    assert replacement.is_primary is True
    assert (
        PublicPartPhoto.objects.get(source_image=original).status
        == PublicPartPhoto.Status.REJECTED
    )
    assert (
        PublicPartPhoto.objects.get(source_image=replacement).status
        == PublicPartPhoto.Status.PUBLISHED
    )


def test_stale_replacement_target_fails_closed_without_new_image(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="stale-target", password="x")
    binding = StaffMessengerBinding.objects.create(
        user=user, operator_key="RIM", provider="telegram", provider_user_id=7804,
        customer_visible_label="Рим", operator_mode=True,
    )
    upload_primary_part_photo(part=part, upload=_image("one.png"), source="telegram", by=user)
    target = upload_additional_part_photo(
        part=part, upload=_image("two.png"), source="telegram", by=user
    )
    sale = Sale.objects.create(
        number="S-STALE-TARGET", status=Sale.Status.COMPLETED,
        customer_name="Тестовый клиент", sold_at=timezone.now(),
    )
    monkeypatch.setattr(operator_console, "_photo_operation_lines", lambda operation, kind: [part])
    manage = operator_console._photo_selection(
        binding=binding, kind="sale", operation_id=sale.pk, part_id=part.pk
    )
    replace = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=manage[1]["inline_keyboard"][1][0]["callback_data"],
    )
    operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=replace[1]["inline_keyboard"][1][0]["callback_data"],
    )
    image_count_before_stale = part.images.count()
    PartTypeImage.objects.filter(pk=target.image.pk).update(is_active=False, is_primary=False)
    result = operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="stale-target", text="", attachment=_photo_attachment("new.png"),
    )
    assert "недоступно" in result[0].lower()
    assert part.images.filter(is_active=True).count() == 1
    assert part.images.count() == image_count_before_stale


def test_replace_service_rolls_back_new_image_when_public_publishing_fails(
    part, django_user_model, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = str(tmp_path)
    user = django_user_model.objects.create_superuser(username="publish-failure", password="x")
    original = upload_primary_part_photo(
        part=part, upload=_image("old.png"), source="telegram", by=user
    )
    monkeypatch.setattr(
        "apps.catalog.photo_pipeline.publish_photo",
        lambda *args, **kwargs: (_ for _ in ()).throw(PublicPhotoError("rendition failed")),
    )
    with pytest.raises(PublicPhotoError, match="rendition failed"):
        replace_part_photo(
            part=part, target_image_id=original.image.pk, upload=_image("new.png"),
            source="telegram", by=user,
        )
    original.image.refresh_from_db()
    assert original.image.is_active is True
    assert part.images.filter(is_active=True).count() == 1
    assert not PartTypeImage.objects.filter(part=part, image__endswith="new.png").exists()


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
