import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.customer_requests import operator_console
from apps.customer_requests.messengers import consume_max_start, issue_max_link
from apps.customer_requests.models import (
    CustomerRequest,
    MaxMessage,
    OperatorNotification,
    StaffMessengerBinding,
)

from .test_telegram_customer_messaging import _operator, _request, build_part


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_pairing_is_one_time_and_provider_identity_is_explicit(db, django_user_model):
    user = _operator(django_user_model, 99001, username="mobile-denis").user
    token = operator_console.issue_pairing_token(
        user=user, provider="telegram", label="Денис", created_by=user
    )

    binding, message = operator_console.consume_pairing(
        provider="telegram", provider_user_id=99001, raw_token=token
    )
    assert binding.user_id == user.pk
    assert "Денис" in message
    second, message = operator_console.consume_pairing(
        provider="telegram", provider_user_id=99002, raw_token=token
    )
    assert second is None
    assert "недействителен" in message


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_operator_console_requires_binding_and_revocation_is_immediate(db, django_user_model):
    user = _operator(django_user_model, 99003, username="bound").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="telegram", provider_user_id=99003, customer_visible_label="Рим"
    )
    assert operator_console.handle_text(
        provider="telegram", provider_user_id=99004, external_id="1", text="/work"
    ) is None
    menu = operator_console.handle_text(
        provider="telegram", provider_user_id=99003, external_id="2", text="/work"
    )
    assert menu[0] == "Рабочее меню PRO-STOR"
    binding.is_active = False
    binding.save(update_fields=["is_active"])
    assert operator_console.handle_callback(
        provider="telegram", provider_user_id=99003, payload="op:l:1"
    ) == ("Недоступно.", None)


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_telegram_operator_to_max_customer_uses_request_transport_and_dedupes(
    db, django_user_model
):
    part = build_part()
    request = _request(part, key="M" * 32, messenger=CustomerRequest.Messenger.MAX)
    customer = issue_max_link(request_id=request.pk).token
    consume_max_start(token=customer, chat_id=99801, user_id=99802)
    user = _operator(django_user_model, 99005, username="cross").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="telegram", provider_user_id=99005, customer_visible_label="Максим"
    )
    operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="work", text="/work"
    )
    result = operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=f"op:c:{request.public_id.hex}"
    )
    assert result[0].startswith(f"Заявка №{request.reference}")
    operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=f"op:r:{request.public_id.hex}"
    )
    first = operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="same-update", text="ответ из Telegram"
    )
    second = operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="same-update", text="ответ из Telegram"
    )
    assert first[0].startswith("Ответ поставлен")
    assert second[0].startswith("Ответ поставлен")
    messages = MaxMessage.objects.filter(
        conversation__request=request, direction=MaxMessage.Direction.OPERATOR
    )
    assert messages.count() == 2
    assert list(messages.values_list("text", flat=True)) == [
        "Вам отвечает Максим.",
        "ответ из Telegram",
    ]
    reply = messages.get(text="ответ из Telegram")
    assert reply.operator_control_source == "telegram"
    assert reply.operator_author_label == "Максим"
    assert reply.recipient_chat_id == 99801


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_new_request_notification_is_one_per_binding(db, django_user_model):
    part = build_part()
    request = _request(part, key="N" * 32, messenger=CustomerRequest.Messenger.TELEGRAM)
    user = _operator(django_user_model, 99006, username="notify").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="max", provider_user_id=99006, customer_visible_label="Владислав"
    )
    operator_console.queue_new_request_notifications(since=request.created_at)
    operator_console.queue_new_request_notifications(since=request.created_at)
    assert OperatorNotification.objects.filter(
        request=request, binding=binding, kind=OperatorNotification.Kind.NEW_REQUEST
    ).count() == 1


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("photo.png", b"\x89PNG\r\n\x1a\noperator"),
        ("photo.jpg", b"\xff\xd8\xffoperator"),
        ("photo.webp", b"RIFF0000WEBPoperator"),
        ("manual.pdf", b"%PDF-1.7\noperator"),
    ],
)
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_operator_attachment_uses_the_same_private_reply_pipeline(
    db, django_user_model, filename, content
):
    part = build_part()
    request = _request(
        part, key=(f"A-{filename}".replace(".", "") + "x" * 32)[:32], messenger="max"
    )
    customer = issue_max_link(request_id=request.pk).token
    consume_max_start(token=customer, chat_id=99811, user_id=99812)
    user = _operator(django_user_model, 99011, username="attachment").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="telegram", provider_user_id=99011, customer_visible_label="Денис"
    )
    operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id="work", text="/work"
    )
    operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=f"op:r:{request.public_id.hex}"
    )
    result = operator_console.handle_text(
        provider="telegram", provider_user_id=binding.provider_user_id,
        external_id=filename, text="", attachment=SimpleUploadedFile(filename, content)
    )
    assert result[0].startswith("Ответ поставлен")
    message = MaxMessage.objects.get(
        conversation__request=request, text="", direction=MaxMessage.Direction.OPERATOR
    )
    assert message.attachment_name == filename
    assert message.attachment_content_type
    assert message.attachment
