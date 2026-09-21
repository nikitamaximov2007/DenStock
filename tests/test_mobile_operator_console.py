import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import close_old_connections, connection, connections
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.customer_requests import operator_console, operator_replies
from apps.customer_requests.max_bot import MaxBotWorker
from apps.customer_requests.messengers import consume_max_start, issue_max_link
from apps.customer_requests.models import (
    CustomerRequest,
    MaxDeliveryStatus,
    MaxMessage,
    OperatorConversationContext,
    OperatorNotification,
    StaffMessengerBinding,
    StaffMessengerPairingToken,
)
from apps.customer_requests.telegram_bot import TelegramBotWorker

from .test_telegram_customer_messaging import FakeBotApi, _operator, _request, build_part


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_pairing_code_has_two_independent_provider_slots(db, django_user_model):
    user = _operator(django_user_model, 99001, username="mobile-denis").user
    token = operator_console.issue_pairing_token(
        user=user, provider="telegram", label="Денис", created_by=user
    )
    row = StaffMessengerPairingToken.objects.get(user=user)
    assert operator_console.is_pairing_code(token)
    assert row.provider == ""
    assert row.token_hash != token
    assert row.telegram_consumed_at is None
    assert row.max_consumed_at is None

    binding, message = operator_console.consume_pairing(
        provider="telegram", provider_user_id=99001, raw_token=token
    )
    assert binding.user_id == user.pk
    assert "Денис" in message
    row.refresh_from_db()
    assert row.telegram_consumed_at is not None
    assert row.max_consumed_at is None
    max_binding, max_message = operator_console.consume_pairing(
        provider="max", provider_user_id=99002, provider_chat_id=88002, raw_token=token
    )
    assert max_binding.user_id == user.pk
    assert "Денис" in max_message
    assert max_binding.delivery_chat_id == 88002
    row.refresh_from_db()
    assert row.max_consumed_at is not None
    assert operator_console.consume_pairing(
        provider="telegram", provider_user_id=99003, raw_token=token
    ) == (None, None)


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_max_first_then_telegram_keeps_max_delivery_identity(db, django_user_model):
    user = _operator(django_user_model, 99004, username="max-first").user
    token = operator_console.issue_pairing_token(user=user, label="Максим", created_by=user)
    max_binding, _ = operator_console.consume_pairing(
        provider="max", provider_user_id=94004, provider_chat_id=88004, raw_token=token
    )
    telegram_binding, _ = operator_console.consume_pairing(
        provider="telegram", provider_user_id=99005, raw_token=token
    )
    max_binding.refresh_from_db()
    assert telegram_binding.user_id == user.pk
    assert max_binding.provider_user_id == 94004
    assert max_binding.delivery_chat_id == 88004


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_pairing_works_with_feature_off_but_operator_mode_does_not(db, django_user_model):
    user = _operator(django_user_model, 99007, username="off-pair").user
    token = operator_console.issue_pairing_token(user=user, label="Рим", created_by=user)
    reply = operator_console.handle_text(
        provider="telegram", provider_user_id=99007, external_id="pair-1", text=token
    )
    assert reply[0] == (
        "Доступ сотрудника подключён.\n\nВы вошли как: Рим\n\n"
        "Рабочий режим пока не активирован."
    )
    assert operator_console.handle_text(
        provider="telegram", provider_user_id=99007, external_id="work", text="/work"
    ) is None


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_invalid_expired_and_consumed_code_like_messages_are_silent(db, django_user_model):
    user = _operator(django_user_model, 99008, username="silent-pair").user
    token = operator_console.issue_pairing_token(user=user, label="Максим", created_by=user)
    StaffMessengerPairingToken.objects.filter(user=user).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    assert operator_console.handle_text(
        provider="max", provider_user_id=94008, provider_chat_id=88008,
        external_id="expired", text=token
    ) is None
    assert operator_console.handle_text(
        provider="max", provider_user_id=94008, provider_chat_id=88008,
        external_id="invalid", text="AAAA-BBBB-CCCC"
    ) is None
    assert operator_console.handle_text(
        provider="max", provider_user_id=94008, provider_chat_id=88008,
        external_id="normal", text="Здравствуйте"
    ) is None


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_revoke_invalidates_unconsumed_slot_and_new_code_repairs(db, django_user_model):
    user = _operator(django_user_model, 99009, username="revoke-pair").user
    token = operator_console.issue_pairing_token(user=user, label="Владислав", created_by=user)
    binding, _ = operator_console.consume_pairing(
        provider="telegram", provider_user_id=99009, raw_token=token
    )
    operator_console.revoke_binding(binding=binding)
    assert operator_console.consume_pairing(
        provider="max", provider_user_id=94009, provider_chat_id=88009, raw_token=token
    ) == (None, None)
    replacement = operator_console.issue_pairing_token(
        user=user, label="Владислав", created_by=user
    )
    repaired, _ = operator_console.consume_pairing(
        provider="max", provider_user_id=94009, provider_chat_id=88009, raw_token=replacement
    )
    assert repaired.user_id == user.pk


@pytest.mark.django_db(transaction=True)
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_telegram_and_max_slots_are_atomic_and_independent(django_user_model):
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL row-lock semantics")
    user = _operator(django_user_model, 99010, username="parallel-pair").user
    token = operator_console.issue_pairing_token(user=user, label="Денис", created_by=user)

    def consume(provider, provider_user_id, provider_chat_id=None):
        close_old_connections()
        try:
            return operator_console.consume_pairing(
                provider=provider,
                provider_user_id=provider_user_id,
                provider_chat_id=provider_chat_id,
                raw_token=token,
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        telegram, max_result = list(
            pool.map(
                lambda args: consume(*args),
                [("telegram", 99010, None), ("max", 94010, 88010)],
            )
        )
    assert telegram[0] is not None
    assert max_result[0] is not None

    race_token = operator_console.issue_pairing_token(
        user=_operator(django_user_model, 99013, username="parallel-race").user,
        label="Денис",
        created_by=user,
    )

    def consume_race(provider_id):
        close_old_connections()
        try:
            return operator_console.consume_pairing(
                provider="telegram", provider_user_id=provider_id, raw_token=race_token
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(
            pool.map(
                consume_race,
                [99011, 99012],
            )
        )
    assert sum(result[0] is not None for result in attempts) == 1


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
def test_stale_callback_cannot_reactivate_customer_mode(db, django_user_model):
    part = build_part()
    request = _request(part, key="S" * 32, messenger=CustomerRequest.Messenger.MAX)
    user = _operator(django_user_model, 99031, username="stale-mode").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="telegram", provider_user_id=99031, customer_visible_label="Денис"
    )
    operator_console.handle_text(
        provider="telegram", provider_user_id=99031, external_id="w", text="/work"
    )
    operator_console.handle_callback(
        provider="telegram", provider_user_id=99031, payload=f"op:c:{request.public_id.hex}"
    )
    operator_console.handle_text(
        provider="telegram", provider_user_id=99031, external_id="c", text="/customer"
    )
    result = operator_console.handle_callback(
        provider="telegram", provider_user_id=99031, payload=f"op:r:{request.public_id.hex}"
    )
    binding.refresh_from_db()
    assert result[0].startswith("Рабочий режим не активен")
    assert binding.operator_mode is False
    assert operator_console.handle_text(
        provider="telegram", provider_user_id=99031, external_id="free", text="не отправляй"
    ) is None
    assert not MaxMessage.objects.filter(conversation__request=request).exists()


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True, CUSTOMER_OPERATOR_CONTEXT_TTL_MINUTES=30)
def test_context_ttl_and_worker_restart_invalidate_context(db, django_user_model):
    user = _operator(django_user_model, 99032, username="context").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="telegram", provider_user_id=99032, customer_visible_label="Рим",
        operator_mode=True,
    )
    request = _request(build_part(), key="C" * 32, messenger=CustomerRequest.Messenger.TELEGRAM)
    context = OperatorConversationContext.objects.create(binding=binding, request=request)
    OperatorConversationContext.objects.filter(pk=context.pk).update(
        updated_at=timezone.now() - timedelta(minutes=31)
    )
    assert operator_console.current_request(binding) is None
    context.refresh_from_db()
    assert context.request_id is None
    operator_console.set_context(binding=binding, request_id=request.pk)
    assert operator_console.invalidate_contexts("telegram") == 1
    binding.refresh_from_db()
    context.refresh_from_db()
    assert binding.operator_mode is False
    assert context.request_id is None


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_revoked_max_binding_can_only_repair_to_same_employee(db, django_user_model):
    user = _operator(django_user_model, 99033, username="repair-a").user
    token = operator_console.issue_pairing_token(
        user=user, provider="max", label="Максим", created_by=user
    )
    binding, _ = operator_console.consume_pairing(
        provider="max", provider_user_id=94001, provider_chat_id=777777777, raw_token=token
    )
    binding.is_active = False
    binding.save(update_fields=["is_active"])
    token = operator_console.issue_pairing_token(
        user=user, provider="max", label="Максим", created_by=user
    )
    repaired, _ = operator_console.consume_pairing(
        provider="max", provider_user_id=94001, provider_chat_id=888888888, raw_token=token
    )
    assert repaired.pk == binding.pk
    assert repaired.is_active is True
    assert repaired.delivery_chat_id == 888888888
    other = _operator(django_user_model, 99034, username="repair-b").user
    token = operator_console.issue_pairing_token(
        user=other, provider="max", label="Владислав", created_by=other
    )
    rejected, message = operator_console.consume_pairing(
        provider="max", provider_user_id=94001, provider_chat_id=999999999, raw_token=token
    )
    assert rejected is None
    assert message is None


@pytest.mark.parametrize(
    ("provider", "provider_user_id", "provider_chat_id"),
    [("telegram", 99041, None), ("max", 94041, 777777741)],
)
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_revoke_and_repair_starts_clean_for_both_providers(
    db, django_user_model, provider, provider_user_id, provider_chat_id
):
    request_a = _request(build_part(), key=f"R{provider_user_id}".ljust(32, "A"), messenger="max")
    customer = issue_max_link(request_id=request_a.pk).token
    consume_max_start(token=customer, chat_id=99841, user_id=99842)
    user = _operator(django_user_model, provider_user_id, username=f"repair-{provider}").user
    token = operator_console.issue_pairing_token(
        user=user, provider=provider, label="Денис", created_by=user
    )
    binding, _ = operator_console.consume_pairing(
        provider=provider,
        provider_user_id=provider_user_id,
        provider_chat_id=provider_chat_id,
        raw_token=token,
    )
    chat_kwargs = {"provider_chat_id": provider_chat_id} if provider_chat_id else {}

    operator_console.handle_text(
        provider=provider, provider_user_id=provider_user_id,
        external_id="work-before-revoke", text="/work", **chat_kwargs
    )
    old_callback = operator_console._callback(binding, "c", request_a.public_id.hex)
    operator_console.handle_callback(
        provider=provider, provider_user_id=provider_user_id, payload=old_callback
    )
    operator_console.handle_text(
        provider=provider, provider_user_id=provider_user_id,
        external_id="reply-before-revoke", text="исторический ответ", **chat_kwargs
    )
    intro = MaxMessage.objects.get(
        text="Вам отвечает Денис, владелец сервиса PRO-STORE."
    )
    intro.delivery_status = MaxDeliveryStatus.SENT
    intro.save(update_fields=["delivery_status"])
    operator_replies.confirm_responder_transition(intro)
    historical = MaxMessage.objects.get(text="исторический ответ")
    historical_identity = (
        historical.operator_user_id,
        historical.operator_control_source,
        historical.operator_author_label,
    )

    operator_console.revoke_binding(binding=binding)
    binding.refresh_from_db()
    context = OperatorConversationContext.objects.get(binding=binding)
    assert binding.is_active is False
    assert binding.operator_mode is False
    assert context.request_id is None

    token = operator_console.issue_pairing_token(
        user=user, provider=provider, label="Денис", created_by=user
    )
    repaired, _ = operator_console.consume_pairing(
        provider=provider,
        provider_user_id=provider_user_id,
        provider_chat_id=provider_chat_id,
        raw_token=token,
    )
    repaired.refresh_from_db()
    context.refresh_from_db()
    assert repaired.pk == binding.pk
    assert repaired.is_active is True
    assert repaired.operator_mode is False
    assert context.request_id is None
    assert (
        MaxMessage.objects.get(pk=historical.pk).operator_user_id,
        MaxMessage.objects.get(pk=historical.pk).operator_control_source,
        MaxMessage.objects.get(pk=historical.pk).operator_author_label,
    ) == historical_identity

    operator_console.handle_text(
        provider=provider, provider_user_id=provider_user_id,
        external_id="work-after-repair", text="/work", **chat_kwargs
    )
    stale = operator_console.handle_callback(
        provider=provider, provider_user_id=provider_user_id, payload=old_callback
    )
    assert stale[0].startswith("Рабочая сессия устарела")
    assert OperatorConversationContext.objects.get(binding=repaired).request_id is None

    before = MaxMessage.objects.count()
    refusal, _ = operator_console.handle_text(
        provider=provider, provider_user_id=provider_user_id,
        external_id="before-selection", text="не отправляй", **chat_kwargs
    )
    assert refusal.startswith("Сначала выберите заявку")
    refusal, _ = operator_console.handle_text(
        provider=provider, provider_user_id=provider_user_id,
        external_id="before-file", text="",
        attachment=SimpleUploadedFile("before.pdf", b"%PDF-1.7\nblocked"), **chat_kwargs
    )
    assert refusal.startswith("Сначала выберите заявку")
    assert MaxMessage.objects.count() == before

    fresh_a = operator_console._callback(repaired, "c", request_a.public_id.hex)
    operator_console.handle_callback(
        provider=provider, provider_user_id=provider_user_id, payload=fresh_a
    )
    result, _ = operator_console.handle_text(
        provider=provider, provider_user_id=provider_user_id,
        external_id="after-selection", text="ответ A", **chat_kwargs
    )
    assert result.startswith("Ответ поставлен")
    assert MaxMessage.objects.filter(text="ответ A", recipient_chat_id=99841).exists()

    request_b = _request(build_part(), key=f"B{provider_user_id}".ljust(32, "B"), messenger="max")
    customer = issue_max_link(request_id=request_b.pk).token
    consume_max_start(token=customer, chat_id=99851, user_id=99852)
    fresh_b = operator_console._callback(repaired, "c", request_b.public_id.hex)
    operator_console.handle_callback(
        provider=provider, provider_user_id=provider_user_id, payload=fresh_b
    )
    result, _ = operator_console.handle_text(
        provider=provider, provider_user_id=provider_user_id,
        external_id="after-switch", text="ответ B", **chat_kwargs
    )
    assert result.startswith("Ответ поставлен")
    assert MaxMessage.objects.filter(text="ответ B", recipient_chat_id=99851).exists()
    assert not MaxMessage.objects.filter(text="ответ B", recipient_chat_id=99841).exists()

    intro_b = MaxMessage.objects.get(
        conversation__request=request_b,
        text="Вам отвечает Денис, владелец сервиса PRO-STORE.",
    )
    intro_b.delivery_status = MaxDeliveryStatus.SENT
    intro_b.save(update_fields=["delivery_status"])
    operator_replies.confirm_responder_transition(intro_b)
    result, _ = operator_console.handle_text(
        provider=provider, provider_user_id=provider_user_id,
        external_id="after-file", text="",
        attachment=SimpleUploadedFile("after.pdf", b"%PDF-1.7\naccepted"), **chat_kwargs
    )
    assert result.startswith("Ответ поставлен")
    assert MaxMessage.objects.filter(
        conversation__request=request_b, attachment_name="after.pdf"
    ).exists()


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
        payload=operator_console._callback(binding, "c", request.public_id.hex)
    )
    assert result[0].startswith(f"Заявка №{request.reference}")
    operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id,
        payload=operator_console._callback(binding, "r", request.public_id.hex)
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
def test_responder_is_confirmed_only_after_intro_delivery(db, django_user_model):
    part = build_part()
    request = _request(part, key="INTRO" * 6 + "12", messenger=CustomerRequest.Messenger.MAX)
    customer = issue_max_link(request_id=request.pk).token
    consume_max_start(token=customer, chat_id=99831, user_id=99832)
    user = _operator(django_user_model, 99036, username="intro").user
    StaffMessengerBinding.objects.create(
        user=user, provider="telegram", provider_user_id=99036, customer_visible_label="Денис"
    )
    operator_console.handle_text(
        provider="telegram", provider_user_id=99036, external_id="work", text="/work"
    )
    operator_console.handle_callback(
        provider="telegram", provider_user_id=99036,
        payload=operator_console._callback(
            StaffMessengerBinding.objects.get(user=user), "r", request.public_id.hex
        )
    )
    operator_console.handle_text(
        provider="telegram", provider_user_id=99036, external_id="reply-1", text="первый"
    )
    request.refresh_from_db()
    assert request.current_responder_label == ""
    assert request.pending_responder_label == "Денис, владелец сервиса PRO-STORE"
    intro = MaxMessage.objects.get(
        conversation__request=request,
        text="Вам отвечает Денис, владелец сервиса PRO-STORE.",
    )
    intro.delivery_status = MaxDeliveryStatus.FAILED
    intro.save(update_fields=["delivery_status"])
    operator_replies.confirm_responder_transition(intro)
    request.refresh_from_db()
    assert request.current_responder_label == ""
    assert request.pending_responder_label == "Денис, владелец сервиса PRO-STORE"
    intro.delivery_status = MaxDeliveryStatus.SENT
    intro.save(update_fields=["delivery_status"])
    operator_replies.confirm_responder_transition(intro)
    request.refresh_from_db()
    assert request.current_responder_label == "Денис, владелец сервиса PRO-STORE"
    assert request.pending_responder_label == ""


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


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_max_operator_notification_uses_delivery_chat_not_provider_user_id(db, django_user_model):
    class FakeMaxApi:
        def __init__(self):
            self.calls = []

        def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return {"body": {"mid": "operator-notice-1"}}

    part = build_part()
    request = _request(part, key="Q" * 32, messenger=CustomerRequest.Messenger.TELEGRAM)
    user = _operator(django_user_model, 99035, username="max-notify").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="max", provider_user_id=94001, delivery_chat_id=777777777,
        customer_visible_label="Максим",
    )
    operator_console.queue_new_request_notifications(since=request.created_at)
    api = FakeMaxApi()
    worker = MaxBotWorker(api, heartbeat_file="")
    worker.pacer.wait = lambda _chat_id: None
    worker.send_operator_console_notifications()
    assert api.calls[0]["chat_id"] == 777777777
    assert api.calls[0]["chat_id"] != binding.provider_user_id
    assert (
        OperatorNotification.objects.get(binding=binding).status
        == OperatorNotification.Status.SENT
    )


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_feature_off_does_not_create_console_state_or_author_metadata(db, django_user_model):
    part = build_part()
    request = _request(part, key="O" * 32, messenger=CustomerRequest.Messenger.MAX)
    customer = issue_max_link(request_id=request.pk).token
    consume_max_start(token=customer, chat_id=99821, user_id=99822)
    user = _operator(django_user_model, 99021, username="off-mode").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="telegram", provider_user_id=99021, customer_visible_label="Денис"
    )

    operator_console.queue_new_request_notifications(since=request.created_at)
    assert not OperatorNotification.objects.filter(request=request).exists()
    assert operator_console.handle_text(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        external_id="off",
        text="/work",
    ) is None

    result = operator_replies.submit_reply(
        request_id=request.pk,
        user=user,
        text="обычный ответ из web",
        key="a" * 32,
        channel=CustomerRequest.Messenger.MAX,
    )
    message = result.message
    request.refresh_from_db()
    assert message.operator_author_label == ""
    assert message.operator_control_source == ""
    assert request.current_responder_label == ""
    assert request.current_responder_control_source == ""
    assert not OperatorNotification.objects.exists()


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_feature_off_keeps_admin_pairing_control_surface_available(
    client, db, django_user_model
):
    admin = _operator(
        django_user_model, 99024, username="off-admin", superuser=True
    ).user
    client.force_login(admin)

    response = client.get(reverse("staff_messenger_bindings"))
    assert response.status_code == 200
    assert "Создать код привязки" in response.content.decode()


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_admin_generates_one_provider_neutral_code(client, db, django_user_model):
    admin = _operator(
        django_user_model, 99025, username="pair-admin", superuser=True
    ).user
    client.force_login(admin)
    response = client.post(
        reverse("staff_messenger_bindings"),
        {"action": "pair", "user_id": admin.pk, "label": "Денис"},
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert "Создать код привязки" in html
    assert "staff-provider" not in html
    token = StaffMessengerPairingToken.objects.get(user=admin)
    assert token.provider == ""
    assert token.telegram_consumed_at is None
    assert token.max_consumed_at is None
    code = re.search(r"[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2}", html).group(0)
    assert operator_console.is_pairing_code(code)


@pytest.mark.parametrize(
    ("providers", "expected"),
    [
        (set(), ("Telegram - Не подключён", "MAX - Не подключён")),
        ({"telegram"}, ("Telegram - Подключён", "MAX - Не подключён")),
        ({"max"}, ("Telegram - Не подключён", "MAX - Подключён")),
        ({"telegram", "max"}, ("Telegram - Подключён", "MAX - Подключён")),
    ],
)
def test_admin_status_shows_both_provider_states_for_each_employee(
    client, db, django_user_model, providers, expected
):
    admin = django_user_model.objects.create_superuser(username="status-admin", password="x" * 12)
    employee = django_user_model.objects.create_user(
        username="status-employee", full_name="Денис", password="x" * 12
    )
    for index, provider in enumerate(sorted(providers), start=1):
        StaffMessengerBinding.objects.create(
            user=employee,
            provider=provider,
            provider_user_id=99100 + index,
            delivery_chat_id=99200 + index if provider == "max" else None,
            customer_visible_label="Денис",
        )
    client.force_login(admin)

    response = client.get(reverse("staff_messenger_bindings"))

    assert response.status_code == 200
    html = response.content.decode()
    assert all(status in html for status in expected)


def test_admin_status_ignores_revoked_bindings_and_keeps_employees_separate(
    client, db, django_user_model
):
    admin = django_user_model.objects.create_superuser(username="status-admin-2", password="x" * 12)
    denis = django_user_model.objects.create_user(
        username="status-denis", full_name="Денис", password="x" * 12
    )
    rim = django_user_model.objects.create_user(
        username="status-rim", full_name="Рим", password="x" * 12
    )
    StaffMessengerBinding.objects.create(
        user=denis,
        provider="telegram",
        provider_user_id=99301,
        customer_visible_label="Денис",
        is_active=False,
    )
    StaffMessengerBinding.objects.create(
        user=denis,
        provider="max",
        provider_user_id=99303,
        delivery_chat_id=99403,
        customer_visible_label="Денис",
        is_active=False,
    )
    StaffMessengerBinding.objects.create(
        user=rim,
        provider="max",
        provider_user_id=99302,
        delivery_chat_id=99402,
        customer_visible_label="Рим",
    )
    client.force_login(admin)

    html = client.get(reverse("staff_messenger_bindings")).content.decode()

    denis_card = html.split("<strong>Денис</strong>", 1)[1].split("</ul>", 1)[0]
    rim_card = html.split("<strong>Рим</strong>", 1)[1].split("</ul>", 1)[0]
    assert "Telegram - Не подключён" in denis_card
    assert "MAX - Не подключён" in denis_card
    assert "Telegram - Не подключён" in rim_card
    assert "MAX - Подключён" in rim_card
    assert "99301" not in html
    assert "99302" not in html
    assert "99303" not in html
    assert "99402" not in html
    assert "99403" not in html
    assert "token_hash" not in html
    assert "—" not in html
    assert "–" not in html


def test_admin_status_access_rules_remain_unchanged(client, db, django_user_model):
    anonymous = client.get(reverse("staff_messenger_bindings"))
    assert anonymous.status_code == 302

    user = django_user_model.objects.create_user(username="status-user", password="x" * 12)
    client.force_login(user)
    assert client.get(reverse("staff_messenger_bindings")).status_code == 403


@pytest.mark.parametrize(
    ("label", "first", "takeover"),
    [
        (
            "Денис",
            "Вам отвечает Денис, владелец сервиса PRO-STORE.",
            "К диалогу подключился Денис, владелец сервиса PRO-STORE.",
        ),
        (
            "Рим",
            "Вам отвечает Рим, владелец сервиса PRO-STORE.",
            "К диалогу подключился Рим, владелец сервиса PRO-STORE.",
        ),
    ],
)
def test_owner_customer_visible_wording_is_exact(label, first, takeover):
    assert operator_replies.customer_visible_operator_label(label) in first
    assert first == f"Вам отвечает {operator_replies.customer_visible_operator_label(label)}."
    assert takeover == (
        f"К диалогу подключился {operator_replies.customer_visible_operator_label(label)}."
    )
    assert "сотрудник" not in first.lower()
    assert "менеджер" not in first.lower()


def test_readiness_requires_only_denis_and_rim(db):
    output = StringIO()
    call_command("check_operator_console_readiness", stdout=output)
    report = output.getvalue()
    assert "Денис:" in report
    assert "Рим:" in report
    assert "Максим:" not in report
    assert "Владислав:" not in report


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_feature_off_worker_restart_ignores_console_notification_work(db, django_user_model):
    part = build_part()
    request = _request(part, key="P" * 32)
    user = _operator(django_user_model, 99022, username="off-worker").user
    binding = StaffMessengerBinding.objects.create(
        user=user, provider="telegram", provider_user_id=99022, customer_visible_label="Рим"
    )
    notification = OperatorNotification.objects.create(
        binding=binding,
        request=request,
        kind=OperatorNotification.Kind.NEW_REQUEST,
        dedupe_key="off-worker-notification",
        status=OperatorNotification.Status.SENDING,
    )

    worker = TelegramBotWorker(
        FakeBotApi(), worker_id="operator-off", poll_timeout=0, heartbeat_file=""
    )
    worker.start()
    notification.refresh_from_db()
    assert notification.status == OperatorNotification.Status.SENDING


def test_readiness_check_redacts_provider_identity_and_never_changes_bindings(
    db, django_user_model
):
    user = _operator(django_user_model, 99023, username="readiness").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider="max",
        provider_user_id=9988776655,
        customer_visible_label="Максим",
    )
    output = StringIO()

    call_command("check_operator_console_readiness", stdout=output)

    binding.refresh_from_db()
    report = output.getvalue()
    assert binding.is_active
    assert "…6655" in report
    assert "9988776655" not in report
    assert "token_hash" not in report


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
        payload=operator_console._callback(binding, "r", request.public_id.hex)
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
