import threading
import uuid

import pytest
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection

from apps.accounts import roles
from apps.ai_support.models import SupportConversation, SupportUsageDay
from apps.ai_support.providers.fake import FakeProvider
from apps.ai_support.services import ProviderCapacity, send_message

pytestmark = pytest.mark.postgresql

if connection.vendor != "postgresql":
    pytest.skip("Run against a staging PostgreSQL DATABASE_URL", allow_module_level=True)


@pytest.mark.django_db(transaction=True)
def test_postgresql_global_gate_serializes_shared_codex_capacity(
    django_user_model, settings, monkeypatch
):
    first = django_user_model.objects.create_user(username="pg-ai-first")
    second = django_user_model.objects.create_user(username="pg-ai-second")
    group = Group.objects.get(name=roles.STOREKEEPER)
    first.groups.add(group)
    second.groups.add(group)
    first_conversation = SupportConversation.objects.create(owner=first)
    second_conversation = SupportConversation.objects.create(owner=second)
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "fake"
    settings.AI_SUPPORT_ALLOW_FAKE_PROVIDER = True
    settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = 1
    entered = threading.Event()
    release = threading.Event()
    failures = []

    class BlockingProvider(FakeProvider):
        def generate(self, request):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError
            return super().generate(request)

    monkeypatch.setattr("apps.ai_support.services.get_provider", lambda: BlockingProvider())

    def first_request():
        close_old_connections()
        try:
            send_message(
                conversation=first_conversation,
                user=first,
                text="Первый PostgreSQL запрос",
                token=uuid.uuid4(),
            )
        except Exception as exc:  # pragma: no cover - reported by assertion below
            failures.append(exc)
        finally:
            close_old_connections()

    thread = threading.Thread(target=first_request)
    thread.start()
    assert entered.wait(timeout=10)
    with pytest.raises(ProviderCapacity):
        send_message(
            conversation=second_conversation,
            user=second,
            text="Второй PostgreSQL запрос",
            token=uuid.uuid4(),
        )
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert failures == []
    assert not SupportUsageDay.objects.filter(active_request_token__isnull=False).exists()
