"""Request photos use the existing messages, private storage and bot outboxes."""
import uuid
from io import BytesIO

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image

from apps.customer_requests import (
    max_bot,
    max_service,
    operator_console,
    operator_replies,
    telegram_bot,
    telegram_service,
    workspace,
)
from apps.customer_requests.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentError,
    validate_photo,
)
from apps.customer_requests.max_bot import MaxBotWorker
from apps.customer_requests.messengers import (
    consume_max_start,
    consume_telegram_start,
    issue_max_link,
    issue_telegram_link,
)
from apps.customer_requests.models import (
    MaxDeliveryStatus,
    MaxMessage,
    OperatorNotification,
    StaffMessengerBinding,
    TelegramDeliveryStatus,
    TelegramMessage,
)
from apps.customer_requests.telegram_api import TelegramApiError, TelegramBotApi
from apps.customer_requests.telegram_bot import TelegramBotWorker

from .max_fake import message_callback
from .test_telegram_customer_messaging import _operator, _request, build_part


def _photo():
    buffer = BytesIO()
    Image.new("RGB", (20, 20), (60, 90, 120)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_large_compressed_photo_is_rejected_before_decode():
    buffer = BytesIO()
    Image.new("RGB", (4000, 4001), (60, 90, 120)).save(buffer, format="PNG")
    upload = SimpleUploadedFile("oversized.png", buffer.getvalue())
    with pytest.raises(AttachmentError, match="Слишком большое изображение"):
        validate_photo(upload)


@pytest.mark.parametrize("channel", ["telegram", "max"])
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_photo_notification_sends_actual_image_with_actions(db, django_user_model, channel):
    request = _request(build_part(), key=("notify-photo-" + channel).ljust(32, "n"),
                       messenger=channel)
    operator = _operator(django_user_model, 99301, username="photo-notice").user
    binding = StaffMessengerBinding.objects.create(
        user=operator, provider=channel, provider_user_id=99301,
        delivery_chat_id=99301 if channel == "max" else None,
        customer_visible_label="Денис",
    )
    model = TelegramMessage if channel == "telegram" else MaxMessage
    conversation = (request.telegram_conversation if channel == "telegram"
                    else max_service.ensure_conversation(request))
    message = model.objects.create(
        conversation=conversation, direction=model.Direction.CUSTOMER,
        text="Подойдёт такой сальник?", attachment_name="photo.jpg",
        attachment_content_type="image/jpeg",
        **({"external_message_id": "photo-notice-max"} if channel == "max" else {}),
    )
    message.attachment.save("photo.jpg", ContentFile(_photo()), save=True)
    notice = operator_console.notification_for(
        request, binding, kind=OperatorNotification.Kind.CUSTOMER_MESSAGE,
        preview=message.text, identity=f"{model.__name__}:{message.pk}", message=message,
    )

    class Api:
        calls = []

        def send_file(self, **kwargs):
            self.calls.append(kwargs)
            return {"message_id": 42}

        def upload_file(self, **kwargs):
            self.calls.append(kwargs)
            return "test-image-token"

        def send_file_token(self, **kwargs):
            self.calls.append(kwargs)
            return {"body": {"mid": "test-image-mid"}}

    api = Api()
    worker = (TelegramBotWorker(api, heartbeat_file="") if channel == "telegram"
              else MaxBotWorker(api, heartbeat_file=""))
    if channel == "max":
        worker.pacer.wait = lambda _chat_id: None
    assert worker.send_operator_console_notifications() == 1
    notice.refresh_from_db()
    assert notice.status == OperatorNotification.Status.SENT
    assert api.calls[0]["content"] == _photo()
    sent = api.calls[-1]
    assert "Подойдёт такой сальник?" in sent["caption"]
    markup = sent["reply_markup"] if channel == "telegram" else sent["buttons"]
    labels = str(markup)
    assert all(label in labels for label in ("Открыть заявку", "История диалога", "Написать"))
    assert worker.send_operator_console_notifications() == 0


@pytest.mark.parametrize("channel", ["telegram", "max"])
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_photo_history_and_web_access(client, db, django_user_model, channel):
    request = _request(build_part(), key=("web-photo-" + channel).ljust(32, "w"),
                       messenger=channel)
    other = _request(build_part(), key=("other-photo-" + channel).ljust(32, "o"),
                     messenger=channel)
    user = _operator(django_user_model, 99311, username="photo-view").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider=channel, provider_user_id=99311,
        customer_visible_label="Денис",
    )
    model = TelegramMessage if channel == "telegram" else MaxMessage
    conversation = (request.telegram_conversation if channel == "telegram"
                    else max_service.ensure_conversation(request))
    message = model.objects.create(
        conversation=conversation, direction=model.Direction.CUSTOMER,
        text="Моя деталь", attachment_name="photo.jpg",
        attachment_content_type="image/jpeg",
        **({"external_message_id": "photo-view-max"} if channel == "max" else {}),
    )
    message.attachment.save("photo.jpg", ContentFile(_photo()), save=True)
    entries = workspace.timeline(request)
    photo_entry = next(entry for entry in entries if entry.has_photo)
    assert photo_entry.text == "Моя деталь"
    history, buttons = operator_console.history_page(request, binding=binding)
    assert "📷 Фотография" in history
    photo_button = next(button for row in buttons["inline_keyboard"] for button in row
                        if button["text"].startswith("📷 Фото"))
    source, found, back = operator_console.photo_for_callback(
        provider=channel, provider_user_id=99311,
        payload=photo_button["callback_data"],
    )
    assert source.pk == message.pk and found.pk == request.pk
    assert "История диалога" in str(back)
    if channel == "telegram":
        outgoing = telegram_bot.handle_update({"callback_query": {
            "id": "photo-view-press", "from": {"id": 99311},
            "data": photo_button["callback_data"],
            "message": {"message_id": 17, "chat": {"id": 99311, "type": "private"}},
        }})
        assert outgoing[-1].photo_payload == photo_button["callback_data"]
    else:
        max_bot.handle_update(message_callback(
            99311, 99311, photo_button["callback_data"]
        ))
        queued = MaxMessage.objects.filter(conversation__isnull=True,
                                            attachment=message.attachment.name).first()
        assert queued is not None
        assert "История диалога" in str(queued.buttons)
    client.force_login(user)
    response = client.get(photo_entry.attachment_url)
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == _photo()
    assert response["Cache-Control"] == "private, no-store"
    wrong = reverse("customer_request_attachment", kwargs={
        "pk": other.pk, "channel": channel, "message_id": message.pk,
    })
    assert client.get(wrong).status_code == 404
    binding.is_active = False
    binding.save(update_fields=["is_active"])
    assert operator_console.photo_for_callback(
        provider=channel, provider_user_id=99311,
        payload=photo_button["callback_data"],
    ) is None
    client.logout()
    assert client.get(photo_entry.attachment_url).status_code == 302


def test_damaged_image_is_rejected():
    with pytest.raises(AttachmentError):
        validate_photo(SimpleUploadedFile("photo.jpg", b"\xff\xd8\xffbroken"))


def test_telegram_download_is_bounded_before_persistence():
    class Response:
        headers = {}

        def read(self, size):
            assert size == MAX_ATTACHMENT_BYTES + 1
            return b"x" * size

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    api = TelegramBotApi("test-only-token", opener=lambda *_args, **_kwargs: Response())
    with pytest.raises(TelegramApiError) as error:
        api.download_file("photos/file.jpg")
    assert error.value.error_code == 413


def test_telegram_staff_photo_without_filename_extension_uses_image_bytes():
    class Api:
        def get_file(self, _file_id):
            return {"file_path": "photos/test", "file_size": len(_photo())}

        def download_file(self, _file_path):
            return _photo()

    worker = TelegramBotWorker(Api(), heartbeat_file="")
    attachment = worker._load_operator_attachment({
        "document": {"file_id": "test", "file_name": "Фото 1"},
    })
    assert attachment.content_type == "image/jpeg"
    assert attachment.filename == "telegram-photo.jpg"
    assert attachment.content == _photo()


@pytest.mark.parametrize("channel", ["telegram", "max"])
def test_anonymization_removes_private_photo_bytes(db, channel):
    request = _request(build_part(), key=uuid.uuid4().hex, messenger=channel)
    model = TelegramMessage if channel == "telegram" else MaxMessage
    conversation = (request.telegram_conversation if channel == "telegram"
                    else max_service.ensure_conversation(request))
    message = model.objects.create(
        conversation=conversation, direction=model.Direction.CUSTOMER,
        text="личное фото", attachment_name="private.jpg",
        attachment_content_type="image/jpeg",
        **({"external_message_id": "private-max-mid"} if channel == "max" else {}),
    )
    message.attachment.save("private.jpg", ContentFile(_photo()), save=True)
    name = message.attachment.name
    assert message.attachment.storage.exists(name)
    if channel == "telegram":
        telegram_service.anonymize_conversation(request)
    else:
        max_service.anonymize_conversation(request)
    message.refresh_from_db()
    assert message.text == "" and not message.attachment
    assert not message.attachment.storage.exists(name)


@pytest.mark.parametrize("channel", ["telegram", "max"])
@override_settings(CUSTOMER_STAFF_INTRODUCTIONS_ENABLED=True)
def test_employee_photo_reply_keeps_intro_and_bytes(db, django_user_model, channel):
    request = _request(build_part(), key=("reply-photo-" + channel).ljust(32, "r"),
                       messenger=channel)
    if channel == "telegram":
        consume_telegram_start(token=issue_telegram_link(request_id=request.pk).token,
                               chat_id=99321, user_id=99321, username="customer")
    else:
        consume_max_start(token=issue_max_link(request_id=request.pk).token,
                          chat_id=99321, user_id=99321)
    user = _operator(django_user_model, 99322, username="photo-replier").user
    result = operator_replies.submit_reply(
        request_id=request.pk, user=user, text="Вот подходящая деталь",
        key=uuid.uuid4().hex, channel=channel,
        attachment=SimpleUploadedFile("part.jpg", _photo()),
    )
    model = TelegramMessage if channel == "telegram" else MaxMessage
    rows = list(model.objects.filter(conversation__request=request).order_by("created_at", "pk"))
    intro = next(row for row in rows if row.text.startswith("К диалогу подключился"))
    assert intro.pk < result.message.pk
    assert result.message.attachment.read() == _photo()
    assert result.message.text == "Вот подходящая деталь"
    second = operator_replies.submit_reply(
        request_id=request.pk, user=user, text="Ещё раз", key=uuid.uuid4().hex,
        channel=channel,
    )
    assert second.created
    assert model.objects.filter(conversation__request=request,
                                text__startswith="К диалогу подключился").count() == 1


@pytest.mark.parametrize("channel", ["telegram", "max"])
@pytest.mark.parametrize("source", ["bot", "web"])
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True,
                   CUSTOMER_STAFF_INTRODUCTIONS_ENABLED=True)
def test_photo_reply_from_bot_or_web_reaches_customer_and_survives(
    client, db, django_user_model, channel, source
):
    request = _request(build_part(), key=uuid.uuid4().hex, messenger=channel)
    if channel == "telegram":
        consume_telegram_start(token=issue_telegram_link(request_id=request.pk).token,
                               chat_id=99331, user_id=99331, username="customer")
    else:
        consume_max_start(token=issue_max_link(request_id=request.pk).token,
                          chat_id=99331, user_id=99331)
    user = _operator(django_user_model, 99332, username="photo-sender").user
    if source == "web":
        client.force_login(user)
        response = client.post(reverse("customer_request_reply", kwargs={"pk": request.pk}), {
            "text": "Вот деталь", "submission_key": uuid.uuid4().hex,
            "attachment": SimpleUploadedFile("part.jpg", _photo()),
        })
        assert response.status_code == 302
    else:
        binding = StaffMessengerBinding.objects.create(
            user=user, provider=channel, provider_user_id=99332,
            delivery_chat_id=99332 if channel == "max" else None,
            customer_visible_label="Денис", operator_mode=True,
        )
        _selected, error = operator_console.set_context(binding=binding, request_id=request.pk)
        assert not error
        result = operator_console.handle_text(
            provider=channel, provider_user_id=99332,
            external_id="photo-send-event", text="Вот деталь",
            attachment=validate_photo(SimpleUploadedFile("part.jpg", _photo())),
        )
        assert result == ("", None)

    class Api:
        calls = []

        def send_message(self, **kwargs):
            self.calls.append(("text", kwargs))
            return {"message_id": len(self.calls), "body": {"mid": str(len(self.calls))}}

        def send_file(self, **kwargs):
            self.calls.append(("image", kwargs))
            return {"message_id": len(self.calls)}

        def upload_file(self, **kwargs):
            self.calls.append(("upload", kwargs))
            return "test-outbound-token"

        def send_file_token(self, **kwargs):
            self.calls.append(("image", kwargs))
            return {"body": {"mid": "test-outbound-image"}}

    api = Api()
    worker = (TelegramBotWorker(api, heartbeat_file="") if channel == "telegram"
              else MaxBotWorker(api, heartbeat_file=""))
    if channel == "max":
        worker.pacer.wait = lambda _chat_id: None
    worker.send_customer_messages(limit=20)
    images = [kwargs for kind, kwargs in api.calls if kind == "image"]
    assert len(images) == 1
    assert images[0]["chat_id"] == 99331
    if channel == "telegram":
        assert images[0]["content"] == _photo()
    assert images[0]["caption"] == "Вот деталь"
    model = TelegramMessage if channel == "telegram" else MaxMessage
    row = model.objects.get(conversation__request=request, text="Вот деталь")
    row.refresh_from_db()
    assert row.delivery_status == (
        TelegramDeliveryStatus.SENT if channel == "telegram" else MaxDeliveryStatus.SENT
    )
    assert row.attachment.read() == _photo()
