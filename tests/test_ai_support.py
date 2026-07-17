import logging
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.ai_support.models import (
    DeveloperTicket,
    SupportAttachment,
    SupportConversation,
    SupportMessage,
    SupportRating,
    SupportRuntimeGate,
    SupportUsageDay,
)
from apps.ai_support.providers.base import SupportResult
from apps.ai_support.providers.fake import FakeProvider
from apps.ai_support.services import (
    ConcurrentRequest,
    ProviderCapacity,
    QuotaExceeded,
    create_ticket,
    send_message,
)
from apps.inventory.models import StockBalance, StockMovement
from apps.sales.models import Sale

PASSWORD = "parol-12345"


@pytest.fixture
def make_support_user(db, django_user_model):
    def make(username, role=roles.STOREKEEPER, *, superuser=False):
        if superuser:
            return django_user_model.objects.create_superuser(username, password=PASSWORD)
        user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return make


@pytest.fixture
def support_user(make_support_user):
    return make_support_user("support-user")


@pytest.fixture
def support_settings(settings, tmp_path):
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "fake"
    settings.AI_SUPPORT_ALLOW_FAKE_PROVIDER = True
    settings.AI_SUPPORT_CODEX_MODEL = "fake-model"
    settings.AI_SUPPORT_CODEX_TIMEOUT_SECONDS = 20
    settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = 1
    settings.DENSTOCK_PUBLIC_BASE_URL = "https://185-250-44-206.sslip.io/"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.AI_SUPPORT_RATE_LIMIT = 5
    settings.AI_SUPPORT_DAILY_REQUEST_LIMIT = 50
    settings.AI_SUPPORT_DAILY_TOKEN_LIMIT = 100000
    return settings


def login(client, user):
    client.force_login(user)


def test_role_capability_map_grants_support_to_roles_and_ticket_management_to_two():
    for role in roles.ALL_ROLES:
        assert roles.USE_AI_SUPPORT in roles.ROLE_CAPABILITIES[role]
    assert roles.MANAGE_AI_SUPPORT_TICKETS in roles.ROLE_CAPABILITIES[roles.ADMIN]
    assert roles.MANAGE_AI_SUPPORT_TICKETS in roles.ROLE_CAPABILITIES[roles.MANAGER]
    assert roles.MANAGE_AI_SUPPORT_TICKETS not in roles.ROLE_CAPABILITIES[roles.STOREKEEPER]
    assert roles.MANAGE_AI_SUPPORT_TICKETS not in roles.ROLE_CAPABILITIES[roles.SELLER]


@pytest.mark.parametrize("role", roles.ALL_ROLES)
def test_all_work_roles_can_open_support(client, make_support_user, role):
    user = make_support_user(f"user-{role}", role)
    login(client, user)
    assert client.get(reverse("ai_support:home")).status_code == 200


def test_plain_user_is_forbidden_and_has_no_sidebar_entry(client, make_support_user):
    user = make_support_user("plain", role=None)
    login(client, user)
    response = client.get(reverse("ai_support:home"))
    assert response.status_code == 403
    assert "ИИ-поддержка" not in client.get(reverse("dashboard")).content.decode()


def test_sidebar_support_is_third_primary_item(client, support_user):
    login(client, support_user)
    html = client.get(reverse("dashboard")).content.decode()
    primary = html.split('class="nav__list nav__list--primary"', 1)[1].split("</ul>", 1)[0]
    assert [primary.index(label) for label in ("Главная", "Поиск", "ИИ-поддержка")] == sorted(
        primary.index(label) for label in ("Главная", "Поиск", "ИИ-поддержка")
    )


def test_conversation_and_message_constraints(support_user):
    conversation = SupportConversation.objects.create(owner=support_user)
    token = uuid.uuid4()
    SupportMessage.objects.create(
        conversation=conversation,
        role=SupportMessage.Role.USER,
        text="Вопрос",
        sequence=1,
        idempotency_token=token,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        SupportMessage.objects.create(
            conversation=conversation,
            role=SupportMessage.Role.USER,
            text="Другой",
            sequence=1,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SupportMessage.objects.create(
            conversation=conversation,
            role=SupportMessage.Role.USER,
            text="Повтор",
            sequence=2,
            idempotency_token=token,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SupportMessage.objects.create(
            conversation=conversation,
            role=SupportMessage.Role.USER,
            text="",
            sequence=3,
        )


def test_conversation_isolation_and_uuid_idor(client, make_support_user):
    owner = make_support_user("owner")
    intruder = make_support_user("intruder")
    conversation = SupportConversation.objects.create(owner=owner)
    login(client, intruder)
    assert client.get(reverse("ai_support:conversation", args=[conversation.id])).status_code == 404
    assert client.get(reverse("ai_support:conversation", args=[uuid.uuid4()])).status_code == 404


def test_post_methods_and_csrf_are_required(make_support_user):
    user = make_support_user("csrf-user")
    conversation = SupportConversation.objects.create(owner=user)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.get(reverse("ai_support:conversation_create")).status_code == 405
    assert client.post(reverse("ai_support:conversation_create")).status_code == 403
    assert client.get(reverse("ai_support:message_send", args=[conversation.id])).status_code == 405


def test_fake_provider_message_flow_is_read_only(support_user, support_settings):
    conversation = SupportConversation.objects.create(owner=support_user)
    before = (StockMovement.objects.count(), StockBalance.objects.count(), Sale.objects.count())
    result = send_message(
        conversation=conversation,
        user=support_user,
        text="Почему не совпадают остатки?",
        token=uuid.uuid4(),
        route_path=reverse("balance_list"),
    )
    assert result.assistant_message.status == SupportMessage.Status.COMPLETED
    assert result.assistant_message.provider == "fake"
    assert (
        StockMovement.objects.count(),
        StockBalance.objects.count(),
        Sale.objects.count(),
    ) == before


def test_timeout_and_quota_ui_keep_manual_ticket_available(
    client, monkeypatch, support_user, support_settings
):
    class TimeoutProvider:
        def generate(self, request):
            return SupportResult(
                text="provider payload is ignored for failures",
                provider="test",
                model="test-model",
                status="failed",
                error_code="provider_timeout",
            )

    monkeypatch.setattr("apps.ai_support.services.get_provider", lambda: TimeoutProvider())
    conversation = SupportConversation.objects.create(owner=support_user)
    login(client, support_user)
    url = reverse("ai_support:message_send", args=[conversation.id])
    response = client.post(
        url,
        {"text": "Почему ошибка?", "idempotency_token": uuid.uuid4()},
        follow=True,
    )
    html = response.content.decode()
    assert "Провайдер не ответил вовремя" in html
    assert 'data-error-code="provider_timeout"' in html
    assert "Создать обращение разработчику" in html

    support_settings.AI_SUPPORT_RATE_LIMIT = 1
    response = client.post(
        url,
        {"text": "Повторный вопрос", "idempotency_token": uuid.uuid4()},
        follow=True,
    )
    assert "Лимит запросов исчерпан" in response.content.decode()
    assert "Создать обращение разработчику" in response.content.decode()


def test_idempotency_returns_existing_result_without_second_provider_call(
    monkeypatch, support_user, support_settings
):
    conversation = SupportConversation.objects.create(owner=support_user)
    calls = 0

    class CountingProvider(FakeProvider):
        def generate(self, request):
            nonlocal calls
            calls += 1
            return super().generate(request)

    monkeypatch.setattr("apps.ai_support.services.get_provider", lambda: CountingProvider())
    token = uuid.uuid4()
    first = send_message(
        conversation=conversation, user=support_user, text="Вопрос", token=token
    )
    second = send_message(
        conversation=conversation, user=support_user, text="Вопрос", token=token
    )
    assert calls == 1
    assert second.duplicate is True
    assert second.user_message == first.user_message
    assert second.assistant_message == first.assistant_message


def test_fresh_active_request_blocks_concurrent_call(support_user, support_settings):
    conversation = SupportConversation.objects.create(owner=support_user)
    SupportUsageDay.objects.create(
        user=support_user,
        date=timezone.localdate(),
        active_request_token=uuid.uuid4(),
        active_started_at=timezone.now(),
    )
    with pytest.raises(ConcurrentRequest):
        send_message(
            conversation=conversation,
            user=support_user,
            text="Второй запрос",
            token=uuid.uuid4(),
        )


def test_previous_day_active_request_still_blocks_user(support_user, support_settings):
    conversation = SupportConversation.objects.create(owner=support_user)
    SupportUsageDay.objects.create(
        user=support_user,
        date=timezone.localdate() - timedelta(days=1),
        active_request_token=uuid.uuid4(),
        active_started_at=timezone.now(),
    )
    with pytest.raises(ConcurrentRequest):
        send_message(
            conversation=conversation,
            user=support_user,
            text="Запрос после полуночи",
            token=uuid.uuid4(),
        )


def test_global_runtime_capacity_blocks_another_user(
    make_support_user, support_settings
):
    first = make_support_user("capacity-first")
    second = make_support_user("capacity-second")
    support_settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = 1
    SupportRuntimeGate.objects.get(pk=1)
    SupportUsageDay.objects.create(
        user=first,
        date=timezone.localdate(),
        active_request_token=uuid.uuid4(),
        active_started_at=timezone.now(),
    )
    conversation = SupportConversation.objects.create(owner=second)
    with pytest.raises(ProviderCapacity):
        send_message(
            conversation=conversation,
            user=second,
            text="Запрос при занятом общем слоте",
            token=uuid.uuid4(),
        )


def test_stale_active_request_is_recovered(support_user, support_settings):
    conversation = SupportConversation.objects.create(owner=support_user)
    stale_token = uuid.uuid4()
    stale = SupportMessage.objects.create(
        conversation=conversation,
        role=SupportMessage.Role.USER,
        text="Зависший запрос",
        sequence=1,
        status=SupportMessage.Status.PROCESSING,
        idempotency_token=stale_token,
    )
    usage = SupportUsageDay.objects.create(
        user=support_user,
        date=timezone.localdate(),
        active_request_token=stale_token,
        active_started_at=timezone.now() - timedelta(minutes=10),
    )
    result = send_message(
        conversation=conversation,
        user=support_user,
        text="Новый запрос",
        token=uuid.uuid4(),
    )
    stale.refresh_from_db()
    usage.refresh_from_db()
    assert stale.status == SupportMessage.Status.FAILED
    assert stale.error_code == "stale_processing"
    assert usage.active_request_token is None
    assert result.assistant_message is not None


@pytest.mark.parametrize(
    "error_code",
    [
        "provider_timeout",
        "provider_unavailable",
        "codex_not_authenticated",
        "codex_cli_incompatible",
        "provider_invalid_output",
        "codex_forbidden_tool_event",
        "provider_output_too_large",
        "codex_invalid_usage",
    ],
)
def test_global_gate_is_released_for_every_provider_failure(
    monkeypatch, support_user, support_settings, error_code
):
    class FailedProvider:
        def generate(self, request):
            return SupportResult(
                text="safe failure",
                provider="codex_cli",
                model="test",
                status="failed",
                error_code=error_code,
            )

    monkeypatch.setattr("apps.ai_support.services.get_provider", lambda: FailedProvider())
    conversation = SupportConversation.objects.create(owner=support_user)
    send_message(
        conversation=conversation,
        user=support_user,
        text="Проверка освобождения слота",
        token=uuid.uuid4(),
    )
    usage = SupportUsageDay.objects.get(user=support_user, date=timezone.localdate())
    assert usage.active_request_token is None
    assert usage.active_started_at is None


def test_invalid_provider_usage_is_not_stored_or_added_to_quota(
    monkeypatch, support_user, support_settings
):
    class InvalidUsageProvider:
        def generate(self, request):
            return SupportResult(
                text="Ответ",
                provider="fixture",
                model="fixture",
                status="completed",
                usage={"input_tokens": True, "output_tokens": 10**30},
            )

    monkeypatch.setattr(
        "apps.ai_support.services.get_provider", lambda: InvalidUsageProvider()
    )
    conversation = SupportConversation.objects.create(owner=support_user)
    result = send_message(
        conversation=conversation,
        user=support_user,
        text="Проверка usage",
        token=uuid.uuid4(),
    )
    usage = SupportUsageDay.objects.get(user=support_user, date=timezone.localdate())
    assert result.assistant_message.usage == {}
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_untrusted_provider_request_id_cannot_inject_logs(
    monkeypatch, caplog, support_user, support_settings
):
    class InjectedRequestIdProvider:
        def generate(self, request):
            return SupportResult(
                text="Ответ",
                provider="fixture",
                model="fixture",
                status="completed",
                request_id="safe\nFORGED_LOG_ENTRY\tvalue",
            )

    monkeypatch.setattr(
        "apps.ai_support.services.get_provider", lambda: InjectedRequestIdProvider()
    )
    conversation = SupportConversation.objects.create(owner=support_user)
    with caplog.at_level(logging.INFO, logger="denstock.ai_support"):
        send_message(
            conversation=conversation,
            user=support_user,
            text="Проверка request id",
            token=uuid.uuid4(),
        )
    assert "FORGED_LOG_ENTRY" not in caplog.text


def test_global_gate_is_released_after_unexpected_provider_exception(
    monkeypatch, support_user, support_settings
):
    class BrokenProvider:
        def generate(self, request):
            raise RuntimeError("internal secret")

    monkeypatch.setattr("apps.ai_support.services.get_provider", lambda: BrokenProvider())
    conversation = SupportConversation.objects.create(owner=support_user)
    result = send_message(
        conversation=conversation,
        user=support_user,
        text="Неожиданная ошибка provider",
        token=uuid.uuid4(),
    )
    usage = SupportUsageDay.objects.get(user=support_user, date=timezone.localdate())
    assert result.assistant_message.status == SupportMessage.Status.FAILED
    assert result.assistant_message.error_code == "provider_unavailable"
    assert usage.active_request_token is None


def test_rate_and_daily_quotas(support_user, support_settings):
    conversation = SupportConversation.objects.create(owner=support_user)
    support_settings.AI_SUPPORT_RATE_LIMIT = 1
    send_message(
        conversation=conversation, user=support_user, text="Первый", token=uuid.uuid4()
    )
    with pytest.raises(QuotaExceeded):
        send_message(
            conversation=conversation, user=support_user, text="Второй", token=uuid.uuid4()
        )
    support_settings.AI_SUPPORT_RATE_LIMIT = 10
    usage = SupportUsageDay.objects.get(user=support_user, date=timezone.localdate())
    usage.request_count = support_settings.AI_SUPPORT_DAILY_REQUEST_LIMIT
    usage.save(update_fields=["request_count"])
    with pytest.raises(QuotaExceeded):
        send_message(
            conversation=conversation, user=support_user, text="Третий", token=uuid.uuid4()
        )


def test_rating_is_one_per_answer(client, support_user):
    conversation = SupportConversation.objects.create(owner=support_user)
    answer = SupportMessage.objects.create(
        conversation=conversation,
        role=SupportMessage.Role.ASSISTANT,
        text="Ответ",
        sequence=1,
        status=SupportMessage.Status.COMPLETED,
    )
    login(client, support_user)
    url = reverse("ai_support:message_rating", args=[answer.id])
    assert client.post(url, {"value": "helpful"}).status_code == 302
    assert client.post(url, {"value": "unhelpful"}).status_code == 302
    assert SupportRating.objects.count() == 1
    assert SupportRating.objects.get().value == "unhelpful"


def test_ticket_snapshot_contains_only_explicit_messages(support_user):
    conversation = SupportConversation.objects.create(owner=support_user)
    question = SupportMessage.objects.create(
        conversation=conversation, role="user", text="Передать", sequence=1
    )
    answer = SupportMessage.objects.create(
        conversation=conversation, role="assistant", text="Не передавать", sequence=2
    )
    SupportMessage.objects.create(
        conversation=conversation, role="user", text="Скрытый текст", sequence=3
    )
    ticket = create_ticket(
        conversation=conversation,
        user=support_user,
        description="Проблема",
        include_question=True,
        question_message=question.id,
        include_answer=False,
        answer_message=answer.id,
    )
    serialized = str(ticket.conversation_snapshot)
    assert "Передать" in serialized
    assert "Не передавать" not in serialized
    assert "Скрытый текст" not in serialized


def test_ticket_manager_permissions_and_no_conversation_access(
    client, make_support_user
):
    owner = make_support_user("ticket-owner")
    manager = make_support_user("manager", roles.MANAGER)
    storekeeper = make_support_user("other-storekeeper")
    conversation = SupportConversation.objects.create(owner=owner)
    ticket = DeveloperTicket.objects.create(
        conversation=conversation,
        author=owner,
        description="Только snapshot",
        conversation_snapshot=[],
        diagnostic_snapshot={},
    )
    login(client, manager)
    assert client.get(reverse("ai_support:ticket_list")).status_code == 200
    assert client.get(reverse("ai_support:ticket_detail", args=[ticket.id])).status_code == 200
    assert client.get(reverse("ai_support:conversation", args=[conversation.id])).status_code == 404
    client.force_login(storekeeper)
    assert client.get(reverse("ai_support:ticket_list")).status_code == 403


def test_feature_disabled_keeps_manual_ticket_available(
    client, support_user, settings
):
    settings.AI_SUPPORT_ENABLED = False
    conversation = SupportConversation.objects.create(owner=support_user)
    login(client, support_user)
    html = client.get(reverse("ai_support:conversation", args=[conversation.id])).content.decode()
    assert "ИИ-поддержка выключена" in html
    response = client.post(
        reverse("ai_support:ticket_create", args=[conversation.id]),
        {"description": "Ручное обращение"},
    )
    assert response.status_code == 302
    assert DeveloperTicket.objects.filter(author=support_user).exists()


def test_purge_command_is_dry_run_by_default_and_confirmed_deletes(
    support_user, settings, tmp_path, capsys
):
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.AI_SUPPORT_ATTACHMENT_RETENTION_DAYS = 30
    settings.AI_SUPPORT_CONVERSATION_RETENTION_DAYS = 180
    conversation = SupportConversation.objects.create(owner=support_user)
    message = SupportMessage.objects.create(
        conversation=conversation,
        role=SupportMessage.Role.USER,
        text="Просроченный скриншот",
        sequence=1,
    )
    relative_path = "ai-support/old.png"
    private_file = settings.PRIVATE_MEDIA_ROOT / relative_path
    private_file.parent.mkdir(parents=True)
    private_file.write_bytes(b"normalized-image")
    SupportAttachment.objects.create(
        message=message,
        relative_path=relative_path,
        sha256="a" * 64,
        size=16,
        mime_type="image/png",
        width=1,
        height=1,
    )
    old = timezone.now() - timedelta(days=200)
    SupportConversation.objects.filter(pk=conversation.pk).update(updated_at=old)
    call_command("purge_ai_support_data")
    assert SupportConversation.objects.filter(pk=conversation.pk).exists()
    assert private_file.exists()
    assert "DRY RUN" in capsys.readouterr().out
    call_command("purge_ai_support_data", "--confirm")
    assert not SupportConversation.objects.filter(pk=conversation.pk).exists()
    assert not private_file.exists()
