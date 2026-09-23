import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, connections
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.customer_requests import operator_console, operator_replies
from apps.customer_requests.max_api import MaxApiError, MaxBotApi, MaxNetworkError
from apps.customer_requests.max_bot import MaxBotWorker
from apps.customer_requests.messengers import (
    consume_max_start,
    consume_telegram_start,
    issue_max_link,
    issue_telegram_link,
)
from apps.customer_requests.models import (
    CustomerRequest,
    MaxDeliveryStatus,
    MaxMessage,
    OperatorConversationContext,
    OperatorNotification,
    StaffMessengerBinding,
    StaffMessengerPairingToken,
    TelegramMessage,
)
from apps.customer_requests.telegram_api import TelegramApiError, TelegramNetworkError
from apps.customer_requests.telegram_bot import TelegramBotWorker

from .max_fake import FAKE_MAX_TOKEN, FakeMaxServer
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


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_enabled_pairing_returns_owner_panel_without_command_instructions(
    db, django_user_model
):
    user = _operator(django_user_model, 99200, username="panel-pair").user
    token = operator_console.issue_pairing_token(user=user, label="Денис", created_by=user)

    reply = operator_console.handle_text(
        provider="telegram", provider_user_id=99200, external_id="pair-panel", text=token
    )

    assert reply[0] == "Панель владельца PRO-STORE"
    assert [row[0]["text"] for row in reply[1]["inline_keyboard"]] == [
        "Все заявки",
        "Новые заявки",
        "Загрузка фото по продажам/ремонтам",
    ]
    assert "/work" not in reply[0]


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_nikita_binding_renders_admin_panel_without_customer_button(db, django_user_model):
    user = _operator(django_user_model, 877307933, username="nikita-admin").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        operator_key="NIKITA",
        provider="telegram",
        provider_user_id=877307933,
        customer_visible_label="NIKITA",
    )

    reply = operator_console.handle_text(
        provider="telegram", provider_user_id=877307933, external_id="nikita-menu",
        text="/start",
    )

    assert reply[0] == "Панель администратора PRO-STORE"
    buttons = [row[0]["text"] for row in reply[1]["inline_keyboard"]]
    assert buttons == ["Все заявки", "Новые заявки", "Загрузка фото по продажам/ремонтам"]
    assert "Мои заявки" not in buttons
    assert operator_console.binding_role(binding) == "ADMIN"


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_admin_binding_takes_precedence_over_customer_menu(db, django_user_model):
    user = _operator(django_user_model, 877307934, username="nikita-precedence").user
    StaffMessengerBinding.objects.create(
        user=user,
        operator_key="NIKITA",
        provider="telegram",
        provider_user_id=877307934,
        customer_visible_label="NIKITA",
    )

    reply = operator_console.handle_text(
        provider="telegram", provider_user_id=877307934, external_id="nikita-my-requests",
        text="Мои заявки",
    )

    assert reply[0] == "Панель администратора PRO-STORE"
    assert "Мои заявки" not in str(reply)


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_explicit_nikita_binding_command_is_idempotent_and_redacts_identity(
    db, django_user_model, capsys
):
    _operator(django_user_model, 877307936, username="admin")

    for _ in range(2):
        call_command(
            "activate_telegram_admin_identity",
            provider_user_id=877307936,
            username="admin",
            confirm=True,
        )

    binding = StaffMessengerBinding.objects.get(provider_user_id=877307936)
    assert binding.operator_key == "NIKITA"
    assert binding.customer_visible_label == "NIKITA"
    assert "877307936" not in capsys.readouterr().out


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_nikita_reply_is_pro_store_to_customer_and_admin_in_audit(
    db, django_user_model
):
    user = _operator(django_user_model, 877307935, username="nikita-reply").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        operator_key="NIKITA",
        provider="telegram",
        provider_user_id=877307935,
        customer_visible_label="NIKITA",
    )
    request = _request(build_part(), key="nikita-role-reply".ljust(32, "n"))
    consume_telegram_start(
        token=issue_telegram_link(request_id=request.pk).token,
        chat_id=700099,
        user_id=700099,
        username="client",
    )
    operator_console.set_context(binding=binding, request_id=request.pk)

    reply = operator_console.handle_text(
        provider="telegram", provider_user_id=877307935, external_id="nikita-reply",
        text="Ответ от администратора",
    )

    assert reply[0] == "Ответ поставлен в очередь доставки клиенту."
    message = TelegramMessage.objects.get(text="Ответ от администратора")
    assert message.operator_author_label == "NIKITA / ADMIN"
    assert message.operator_control_source == "telegram"
    intro = TelegramMessage.objects.get(text="Вам отвечает PRO-STORE.")
    assert intro.operator_author_label == "PRO-STORE"


@pytest.mark.parametrize(
    ("provider", "provider_user_id", "provider_chat_id"),
    [("telegram", 99201, None), ("max", 94201, 88201)],
)
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_owner_panel_buttons_activate_console_for_both_providers(
    db, django_user_model, provider, provider_user_id, provider_chat_id
):
    user = _operator(django_user_model, provider_user_id, username=f"panel-{provider}").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider=provider,
        provider_user_id=provider_user_id,
        delivery_chat_id=provider_chat_id,
        customer_visible_label="Денис",
    )
    text, markup = operator_console.owner_panel(binding)

    assert text == "Панель владельца PRO-STORE"
    expected = ["Все заявки", "Новые заявки", "Загрузка фото по продажам/ремонтам"]
    assert [row[0]["text"] for row in markup["inline_keyboard"]] == expected
    assert binding.operator_mode is False

    result = operator_console.handle_callback(
        provider=provider,
        provider_user_id=provider_user_id,
        payload=markup["inline_keyboard"][0][0]["callback_data"],
    )

    binding.refresh_from_db()
    assert result[0] == "Заявок нет."
    assert binding.operator_mode is True


def test_max_owner_markup_uses_max_payloads(db, django_user_model):
    user = _operator(django_user_model, 94202, username="max-markup").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider="max",
        provider_user_id=94202,
        delivery_chat_id=88202,
        customer_visible_label="Рим",
    )
    _text, telegram_markup = operator_console.owner_panel(binding)

    markup = operator_console.buttons_for_provider(telegram_markup, "max")

    assert markup["inline_keyboard"][0][0]["payload"].startswith("op:l:")
    assert "callback_data" not in markup["inline_keyboard"][0][0]


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_owner_panel_delivery_command_queues_one_panel_per_active_owner(
    db, django_user_model, capsys
):
    user = _operator(django_user_model, 99203, username="panel-send").user
    telegram = StaffMessengerBinding.objects.create(
        user=user,
        provider="telegram",
        provider_user_id=99203,
        customer_visible_label="Денис",
    )
    max_binding = StaffMessengerBinding.objects.create(
        user=user,
        provider="max",
        provider_user_id=94203,
        delivery_chat_id=88203,
        customer_visible_label="Денис",
    )
    call_command("send_owner_console_panel")

    rows = OperatorNotification.objects.filter(
        kind=OperatorNotification.Kind.OWNER_PANEL
    ).order_by("binding_id")
    assert rows.count() == 2
    assert set(rows.values_list("binding_id", flat=True)) == {telegram.pk, max_binding.pk}
    assert rows.filter(status=OperatorNotification.Status.PENDING).count() == 2
    assert "Панелей поставлено в очередь: 2" in capsys.readouterr().out

    call_command("send_owner_console_panel")
    assert OperatorNotification.objects.filter(
        kind=OperatorNotification.Kind.OWNER_PANEL
    ).count() == 2

    call_command("send_owner_console_panel", refresh=True)
    assert OperatorNotification.objects.filter(
        kind=OperatorNotification.Kind.OWNER_PANEL,
        status=OperatorNotification.Status.PENDING,
    ).count() == 2


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_owner_panel_delivery_command_is_safe_when_feature_is_off(db):
    with pytest.raises(CommandError, match="панель владельца не отправлена"):
        call_command("send_owner_console_panel")


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_provider_workers_deliver_queued_panel_with_native_button_shapes(
    db, django_user_model, monkeypatch
):
    Group.objects.get_or_create(name="Продавец/Мастер")
    user = _operator(django_user_model, 99204, username="panel-workers").user
    telegram = StaffMessengerBinding.objects.create(
        user=user,
        provider="telegram",
        provider_user_id=99204,
        customer_visible_label="Денис",
    )
    max_binding = StaffMessengerBinding.objects.create(
        user=user,
        provider="max",
        provider_user_id=94204,
        delivery_chat_id=88204,
        customer_visible_label="Рим",
    )
    operator_console.queue_owner_panel(binding=telegram)
    operator_console.queue_owner_panel(binding=max_binding)

    active_notification_transactions = 0
    original_atomic = operator_console.transaction.atomic

    @contextmanager
    def tracked_atomic(*args, **kwargs):
        nonlocal active_notification_transactions
        with original_atomic(*args, **kwargs):
            active_notification_transactions += 1
            try:
                yield
            finally:
                active_notification_transactions -= 1

    monkeypatch.setattr(operator_console.transaction, "atomic", tracked_atomic)

    class TransactionAwareTelegramApi(FakeBotApi):
        def send_message(self, **kwargs):
            assert active_notification_transactions == 0
            return super().send_message(**kwargs)

    telegram_api = TransactionAwareTelegramApi()

    class FakeMaxApi:
        def __init__(self):
            self.calls = []

        def send_message(self, **kwargs):
            assert active_notification_transactions == 0
            self.calls.append(kwargs)
            return {"body": {"mid": "panel-worker-max"}}

    max_api = FakeMaxApi()
    telegram_worker = TelegramBotWorker(telegram_api, heartbeat_file="")
    max_worker = MaxBotWorker(max_api, heartbeat_file="")
    max_worker.pacer.wait = lambda _chat_id: None

    original_binding_for = operator_console.binding_for
    locked_in_transaction = []

    def checked_binding_for(*args, **kwargs):
        if kwargs.get("lock"):
            locked_in_transaction.append(active_notification_transactions > 0)
        return original_binding_for(*args, **kwargs)

    monkeypatch.setattr(operator_console, "binding_for", checked_binding_for)

    assert telegram_worker.send_operator_console_notifications() == 1
    assert max_worker.send_operator_console_notifications() == 1
    assert locked_in_transaction == [True, True]
    telegram_payload = telegram_api.sent[0]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    assert telegram_payload.startswith("op:l:")
    assert max_api.calls[0]["buttons"][0][0]["payload"].startswith("op:l:")
    assert "callback_data" not in max_api.calls[0]["buttons"][0][0]
    assert OperatorNotification.objects.filter(
        kind=OperatorNotification.Kind.OWNER_PANEL,
        status=OperatorNotification.Status.SENT,
    ).count() == 2
    assert OperatorNotification.objects.get(binding=telegram).external_message_id
    assert OperatorNotification.objects.get(binding=max_binding).external_message_id


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_max_owner_panel_real_serializer_and_callback_round_trip(
    db, django_user_model
):
    user = _operator(django_user_model, 99206, username="panel-max-real").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider="max",
        provider_user_id=94206,
        delivery_chat_id=88206,
        customer_visible_label="Денис",
    )
    operator_console.queue_owner_panel(binding=binding)
    server = FakeMaxServer()
    server.start()
    try:
        api = MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=2)
        worker = MaxBotWorker(api, worker_id="max-real-panel", heartbeat_file="")
        worker.pacer.wait = lambda _chat_id: None

        assert worker.send_operator_console_notifications() == 1

        sent = server.sent[0]
        assert sent["chat_id"] == binding.delivery_chat_id
        assert sent["chat_id"] != binding.provider_user_id
        assert sent["text"] == "Панель владельца PRO-STORE"
        buttons = sent["attachments"][0]["payload"]["buttons"]
        assert [button["text"] for row in buttons for button in row] == [
            "Все заявки",
            "Новые заявки",
            "Загрузка фото по продажам/ремонтам",
        ]
        assert all(
            "callback_data" not in button
            and button["payload"].startswith("op:")
            for row in buttons
            for button in row
        )

        first_result = operator_console.handle_callback(
            provider="max",
            provider_user_id=binding.provider_user_id,
            payload=buttons[0][0]["payload"],
        )
        second_result = operator_console.handle_callback(
            provider="max",
            provider_user_id=binding.provider_user_id,
            payload=buttons[1][0]["payload"],
        )
        assert first_result[0] == "Заявок нет."
        assert second_result[0] == "Новых заявок нет."
        assert OperatorConversationContext.objects.filter(
            binding=binding, request__isnull=False
        ).count() == 0
        notification = OperatorNotification.objects.get(binding=binding)
        assert notification.status == OperatorNotification.Status.SENT
        assert notification.external_message_id
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("provider", "api_error"),
    [
        ("telegram", TelegramApiError(400, "refused")),
        ("max", MaxApiError(400, "refused", "refused")),
    ],
)
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_owner_panel_explicit_provider_failure_returns_to_recoverable_state(
    db, django_user_model, provider, api_error
):
    user = _operator(django_user_model, 99214, username=f"panel-error-{provider}").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider=provider,
        provider_user_id=99214,
        delivery_chat_id=88214 if provider == "max" else None,
        customer_visible_label="Денис",
    )
    row, _created = operator_console.queue_owner_panel(binding=binding)

    class FailingApi:
        def send_message(self, **_kwargs):
            raise api_error

    if provider == "telegram":
        worker = TelegramBotWorker(FailingApi(), heartbeat_file="")
    else:
        worker = MaxBotWorker(FailingApi(), heartbeat_file="")
        worker.pacer.wait = lambda _chat_id: None
    worker.send_operator_console_notifications()

    row.refresh_from_db()
    assert row.status == OperatorNotification.Status.PENDING
    assert row.external_message_id == ""


@pytest.mark.parametrize("provider", ["telegram", "max"])
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_owner_panel_pre_provider_exception_never_stays_sending(
    db, django_user_model, monkeypatch, provider
):
    user = _operator(django_user_model, 99215, username=f"panel-prepare-{provider}").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider=provider,
        provider_user_id=99215,
        delivery_chat_id=88215 if provider == "max" else None,
        customer_visible_label="Рим",
    )
    row, _created = operator_console.queue_owner_panel(binding=binding)
    monkeypatch.setattr(
        operator_console,
        "prepare_notification_delivery",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("before provider")),
    )

    if provider == "telegram":
        worker = TelegramBotWorker(FakeBotApi(), heartbeat_file="")
    else:
        worker = MaxBotWorker(object(), heartbeat_file="")
        worker.pacer.wait = lambda _chat_id: None
    worker.send_operator_console_notifications()

    row.refresh_from_db()
    assert row.status == OperatorNotification.Status.PENDING
    assert row.external_message_id == ""


@pytest.mark.parametrize(
    ("provider", "api_error"),
    [
        ("telegram", TelegramNetworkError("timeout", ambiguous=True)),
        ("max", MaxNetworkError("timeout", ambiguous=True)),
    ],
)
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_owner_panel_ambiguous_provider_result_is_never_retried(
    db, django_user_model, provider, api_error
):
    user = _operator(django_user_model, 99216, username=f"panel-ambiguous-{provider}").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider=provider,
        provider_user_id=99216,
        delivery_chat_id=88216 if provider == "max" else None,
        customer_visible_label="Денис",
    )
    row, _created = operator_console.queue_owner_panel(binding=binding)

    class AmbiguousApi:
        def send_message(self, **_kwargs):
            raise api_error

    if provider == "telegram":
        worker = TelegramBotWorker(AmbiguousApi(), heartbeat_file="")
    else:
        worker = MaxBotWorker(AmbiguousApi(), heartbeat_file="")
        worker.pacer.wait = lambda _chat_id: None
    worker.send_operator_console_notifications()

    row.refresh_from_db()
    assert row.status == OperatorNotification.Status.UNCERTAIN, row.last_error
    assert row.external_message_id == ""


@pytest.mark.parametrize("provider", ["telegram", "max"])
@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_revoked_owner_panel_binding_fails_closed_before_provider_send(
    db, django_user_model, provider
):
    user = _operator(django_user_model, 99217, username=f"panel-revoked-{provider}").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider=provider,
        provider_user_id=99217,
        delivery_chat_id=88217 if provider == "max" else None,
        customer_visible_label="Рим",
    )
    row, _created = operator_console.queue_owner_panel(binding=binding)
    binding.is_active = False
    binding.save(update_fields=["is_active", "updated_at"])

    class RecordingApi:
        calls = 0

        def send_message(self, **_kwargs):
            self.calls += 1
            return {}

    api = RecordingApi()
    if provider == "telegram":
        worker = TelegramBotWorker(api, heartbeat_file="")
    else:
        worker = MaxBotWorker(api, heartbeat_file="")
        worker.pacer.wait = lambda _chat_id: None
    worker.send_operator_console_notifications()

    row.refresh_from_db()
    assert api.calls == 0
    assert row.status == OperatorNotification.Status.FAILED


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_explicit_recovery_marks_only_proven_owner_panel_attempts_uncertain(
    db, django_user_model, capsys
):
    user = _operator(django_user_model, 99218, username="panel-recovery").user
    binding = StaffMessengerBinding.objects.create(
        user=user,
        provider="telegram",
        provider_user_id=99218,
        customer_visible_label="Денис",
    )
    row = OperatorNotification.objects.create(
        binding=binding,
        request=None,
        kind=OperatorNotification.Kind.OWNER_PANEL,
        dedupe_key=f"incident-owner-panel:{binding.pk}",
        status=OperatorNotification.Status.SENDING,
    )
    other = OperatorNotification.objects.create(
        binding=binding,
        request=None,
        kind=OperatorNotification.Kind.OWNER_PANEL,
        dedupe_key=f"other-owner-panel:{binding.pk}",
        status=OperatorNotification.Status.SENDING,
    )

    call_command("recover_owner_console_panel", "--notification-id", row.pk)

    row.refresh_from_db()
    other.refresh_from_db()
    assert row.status == OperatorNotification.Status.UNCERTAIN
    assert row.external_message_id == ""
    assert other.status == OperatorNotification.Status.SENDING
    assert "Панелей помечено для явного повтора: 1" in capsys.readouterr().out


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_shared_auth_user_keeps_den_is_and_rim_identities_separate(db, django_user_model):
    admin = django_user_model.objects.create_superuser(username="shared-admin", password="x" * 12)
    denis_code = operator_console.issue_pairing_token(
        user=admin, label="Денис", created_by=admin, operator_key="DENIS"
    )
    rim_code = operator_console.issue_pairing_token(
        user=admin, label="Рим", created_by=admin, operator_key="RIM"
    )

    denis_tg, _ = operator_console.consume_pairing(
        provider="telegram", provider_user_id=99101, raw_token=denis_code
    )
    rim_tg, _ = operator_console.consume_pairing(
        provider="telegram", provider_user_id=99102, raw_token=rim_code
    )

    assert denis_tg.user_id == admin.pk
    assert denis_tg.operator_key == "DENIS"
    assert denis_tg.customer_visible_label == "Денис"
    assert rim_tg.user_id == admin.pk
    assert rim_tg.operator_key == "RIM"
    assert rim_tg.customer_visible_label == "Рим"
    assert StaffMessengerBinding.objects.filter(user=admin, provider="telegram").count() == 2


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
        "Доступ владельца подключён.\n\nВы вошли как: Рим\n\n"
        "Рабочая панель пока не активирована."
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
    Group.objects.get_or_create(name="Продавец/Мастер")
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
    assert menu[0] == "Панель владельца PRO-STORE"
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
    assert result[0].startswith("Рабочая сессия устарела")
    assert binding.operator_mode is False
    reply = operator_console.handle_text(
        provider="telegram", provider_user_id=99031, external_id="free", text="не отправляй"
    )
    assert reply[0] == "Откройте рабочую панель для работы с заявками."
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


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=True)
def test_worker_restart_does_not_create_owner_panel_automatically(db, django_user_model):
    user = _operator(django_user_model, 99205, username="restart-panel").user
    StaffMessengerBinding.objects.create(
        user=user,
        provider="telegram",
        provider_user_id=99205,
        customer_visible_label="Рим",
    )

    worker = TelegramBotWorker(
        FakeBotApi(), worker_id="operator-panel-restart", poll_timeout=0, heartbeat_file=""
    )
    worker.start()

    assert not OperatorNotification.objects.filter(
        kind=OperatorNotification.Kind.OWNER_PANEL
    ).exists()


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
    _panel_text, panel = operator_console.owner_panel(binding)
    _list_text, request_list = operator_console.handle_callback(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        payload=panel["inline_keyboard"][0][0]["callback_data"],
    )
    request_button = request_list["inline_keyboard"][0][0]["callback_data"]
    _card_text, card_buttons = operator_console.handle_callback(
        provider="telegram",
        provider_user_id=binding.provider_user_id,
        payload=request_button,
    )
    reply_button = card_buttons["inline_keyboard"][0][0]["callback_data"]
    operator_console.handle_callback(
        provider="telegram", provider_user_id=binding.provider_user_id, payload=reply_button
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
