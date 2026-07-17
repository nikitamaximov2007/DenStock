import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .diagnostics import canonical_public_url, safe_diagnostic_snapshot, safe_route_context
from .files import NormalizedImage, delete_private_file, normalize_image, save_normalized_image
from .knowledge import retrieve
from .models import (
    DeveloperTicket,
    SupportAttachment,
    SupportConversation,
    SupportMessage,
    SupportRuntimeGate,
    SupportUsageDay,
)
from .prompts import build_system_instruction
from .providers import get_provider
from .providers.base import SupportImage, SupportRequest, SupportResult, SupportTurn

logger = logging.getLogger("denstock.ai_support")


class SupportError(Exception):
    code = "support_error"


class FeatureDisabled(SupportError):
    code = "feature_disabled"


class QuotaExceeded(SupportError):
    code = "quota_exceeded"


class ConcurrentRequest(SupportError):
    code = "concurrent_request"


class ProviderCapacity(SupportError):
    code = "provider_capacity"


class ImageRejected(SupportError):
    code = "rejected_image"


@dataclass(frozen=True)
class SupportFlowResult:
    user_message: SupportMessage
    assistant_message: SupportMessage | None
    duplicate: bool = False


SAFE_ERROR_TEXT = {
    "provider_timeout": "Провайдер не ответил вовремя.",
    "provider_rate_limited": "Провайдер временно ограничил запросы.",
    "provider_server_error": "Провайдер временно недоступен.",
    "provider_unavailable": "Нет связи с провайдером.",
    "provider_rejected": "Провайдер отклонил запрос.",
    "invalid_response": "Провайдер вернул некорректный ответ.",
    "provider_not_configured": "Провайдер ИИ не настроен.",
    "provider_disabled": "Провайдер ИИ выключен.",
    "feature_disabled": "ИИ-поддержка выключена.",
    "codex_auth_missing": "ИИ-поддержка временно не настроена.",
    "subscription_quota_exceeded": "Лимит Codex в подписке временно исчерпан.",
    "provider_output_too_large": "Ответ ИИ превысил безопасный размер.",
    "provider_input_too_large": "Запрос превысил безопасный размер.",
    "provider_invalid_output": "ИИ-поддержка вернула некорректный ответ.",
    "provider_tool_event": "ИИ-поддержка остановила небезопасное действие.",
    "provider_capacity": "ИИ-поддержка сейчас занята.",
}


def _stale_before():
    seconds = max(settings.AI_SUPPORT_CODEX_TIMEOUT_SECONDS * 2, 120)
    return timezone.now() - timedelta(seconds=seconds)


def _recover_stale_usage(usage: SupportUsageDay) -> None:
    if usage.active_request_token and (
        not usage.active_started_at or usage.active_started_at < _stale_before()
    ):
        SupportMessage.objects.filter(
            conversation__owner=usage.user,
            idempotency_token=usage.active_request_token,
            status=SupportMessage.Status.PROCESSING,
        ).update(status=SupportMessage.Status.FAILED, error_code="stale_processing")
        usage.active_request_token = None
        usage.active_started_at = None
        usage.save(update_fields=["active_request_token", "active_started_at", "updated_at"])


def _claim_request(*, conversation, user, token: uuid.UUID, text: str):
    with transaction.atomic():
        SupportRuntimeGate.objects.select_for_update().get(pk=1)
        usage, _ = SupportUsageDay.objects.select_for_update().get_or_create(
            user=user, date=timezone.localdate()
        )
        _recover_stale_usage(usage)
        previous_active = (
            SupportUsageDay.objects.select_for_update()
            .filter(user=user, active_request_token__isnull=False)
            .exclude(pk=usage.pk)
            .first()
        )
        if previous_active:
            _recover_stale_usage(previous_active)
            if previous_active.active_request_token:
                raise ConcurrentRequest
        active_rows = list(
            SupportUsageDay.objects.select_for_update().filter(
                active_request_token__isnull=False
            )
        )
        for active_row in active_rows:
            _recover_stale_usage(active_row)
        active_count = sum(bool(row.active_request_token) for row in active_rows)
        if active_count >= settings.AI_SUPPORT_CODEX_MAX_CONCURRENT:
            raise ProviderCapacity
        locked_conversation = SupportConversation.objects.select_for_update().get(
            pk=conversation.pk, owner=user
        )
        duplicate = SupportMessage.objects.filter(
            conversation=locked_conversation,
            role=SupportMessage.Role.USER,
            idempotency_token=token,
        ).first()
        if duplicate:
            assistant = SupportMessage.objects.filter(
                conversation=locked_conversation,
                role=SupportMessage.Role.ASSISTANT,
                sequence=duplicate.sequence + 1,
            ).first()
            return duplicate, assistant, True
        if usage.active_request_token:
            raise ConcurrentRequest
        minute_ago = timezone.now() - timedelta(minutes=1)
        recent = SupportMessage.objects.filter(
            conversation__owner=user,
            role=SupportMessage.Role.USER,
            created_at__gte=minute_ago,
        ).count()
        if recent >= settings.AI_SUPPORT_RATE_LIMIT:
            raise QuotaExceeded
        if usage.request_count >= settings.AI_SUPPORT_DAILY_REQUEST_LIMIT:
            raise QuotaExceeded
        if usage.input_tokens + usage.output_tokens >= settings.AI_SUPPORT_DAILY_TOKEN_LIMIT:
            raise QuotaExceeded
        sequence = (
            locked_conversation.messages.aggregate(value=Max("sequence"))["value"] or 0
        ) + 1
        message = SupportMessage.objects.create(
            conversation=locked_conversation,
            role=SupportMessage.Role.USER,
            text=text,
            sequence=sequence,
            status=SupportMessage.Status.PROCESSING,
            idempotency_token=token,
        )
        if not locked_conversation.title:
            locked_conversation.title = text[:157] + ("..." if len(text) > 157 else "")
            locked_conversation.save(update_fields=["title", "updated_at"])
        usage.request_count += 1
        usage.active_request_token = token
        usage.active_started_at = timezone.now()
        usage.save(
            update_fields=[
                "request_count",
                "active_request_token",
                "active_started_at",
                "updated_at",
            ]
        )
        return message, None, False


def _release_request(
    token: uuid.UUID, *, user, usage: dict[str, int] | None = None
) -> None:
    with transaction.atomic():
        row = SupportUsageDay.objects.select_for_update().filter(
            user=user,
            active_request_token=token,
        ).first()
        if not row:
            return
        usage = usage or {}
        row.input_tokens += max(int(usage.get("input_tokens", 0) or 0), 0)
        row.output_tokens += max(int(usage.get("output_tokens", 0) or 0), 0)
        row.active_request_token = None
        row.active_started_at = None
        row.save(
            update_fields=[
                "input_tokens",
                "output_tokens",
                "active_request_token",
                "active_started_at",
                "updated_at",
            ]
        )


def _attach_image(message: SupportMessage, image: NormalizedImage) -> SupportAttachment:
    relative_path = save_normalized_image(image)
    try:
        return SupportAttachment.objects.create(
            message=message,
            relative_path=relative_path,
            sha256=image.sha256,
            size=len(image.content),
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            shared_with_provider_at=timezone.now(),
        )
    except Exception:
        delete_private_file(relative_path)
        raise


def _history_for(message: SupportMessage) -> tuple[SupportTurn, ...]:
    rows = list(
        message.conversation.messages.filter(
            sequence__lt=message.sequence, status=SupportMessage.Status.COMPLETED
        ).order_by("-sequence")[:8]
    )
    rows.reverse()
    return tuple(SupportTurn(role=row.role, text=row.text[:4000]) for row in rows)


def _request_for(
    *, message: SupportMessage, user, route_path: str, image: NormalizedImage | None
) -> SupportRequest:
    chunks = retrieve(message.text)
    route_context = safe_route_context(route_path)
    roles = sorted(user.role_names) if not user.is_superuser else ["Администратор"]
    return SupportRequest(
        user_text=message.text,
        system_instruction=build_system_instruction(chunks),
        knowledge_chunks=tuple(chunk.text for chunk in chunks),
        route_context=route_context,
        user_role=", ".join(roles),
        public_base_url=canonical_public_url(),
        max_output_tokens=settings.AI_SUPPORT_MAX_OUTPUT_TOKENS,
        history=_history_for(message),
        image=(SupportImage(image.content, image.mime_type) if image else None),
    )


def _provider_failure(code: str = "provider_unavailable") -> SupportResult:
    return SupportResult(
        text=(
            f"{SAFE_ERROR_TEXT.get(code, 'ИИ-поддержка временно недоступна')} "
            "Вы можете создать ручное обращение разработчику."
        ),
        provider=str(settings.AI_SUPPORT_PROVIDER or "disabled")[:40],
        model=str(settings.AI_SUPPORT_CODEX_MODEL or "")[:120],
        status="failed",
        error_code=code,
    )


def _safe_result_text(result: SupportResult) -> str:
    if result.status == "completed" and result.text.strip():
        return result.text
    return (
        f"{SAFE_ERROR_TEXT.get(result.error_code, 'ИИ-поддержка временно недоступна')} "
        "Вы можете создать ручное обращение разработчику."
    )


def send_message(
    *, conversation, user, text: str, token: uuid.UUID, route_path: str = "", upload=None,
    image_consent: bool = False,
) -> SupportFlowResult:
    if not settings.AI_SUPPORT_ENABLED:
        raise FeatureDisabled
    text = (text or "").strip()
    if not text or len(text) > settings.AI_SUPPORT_MAX_MESSAGE_CHARS:
        raise ValidationError("Сообщение пустое или превышает допустимую длину.")
    normalized = None
    if upload:
        if not image_consent:
            raise ImageRejected
        try:
            normalized = normalize_image(upload)
        except ValidationError as exc:
            raise ImageRejected from exc
    message, assistant, duplicate = _claim_request(
        conversation=conversation, user=user, token=token, text=text
    )
    if duplicate:
        return SupportFlowResult(message, assistant, duplicate=True)

    try:
        if normalized:
            _attach_image(message, normalized)
        request = _request_for(
            message=message, user=user, route_path=route_path, image=normalized
        )
        try:
            result = get_provider().generate(request)
        except Exception:
            result = _provider_failure()
        assistant_status = (
            SupportMessage.Status.COMPLETED
            if result.status == "completed"
            else SupportMessage.Status.FAILED
        )
        with transaction.atomic():
            locked = SupportMessage.objects.select_for_update().get(pk=message.pk)
            assistant = SupportMessage.objects.create(
                conversation=locked.conversation,
                role=SupportMessage.Role.ASSISTANT,
                text=_safe_result_text(result)[:16000],
                sequence=locked.sequence + 1,
                status=assistant_status,
                provider=result.provider[:40],
                model=result.model[:120],
                latency_ms=max(result.latency_ms, 0),
                usage=result.usage,
                error_code=result.error_code[:64],
            )
            locked.status = assistant_status
            locked.error_code = result.error_code[:64]
            locked.save(update_fields=["status", "error_code"])
            locked.conversation.save(update_fields=["updated_at"])
        _release_request(token, user=user, usage=result.usage)
        logger.info(
            "ai_support_request conversation=%s message=%s user=%s provider=%s model=%s "
            "status=%s latency_ms=%s input_tokens=%s output_tokens=%s request_id=%s error=%s",
            message.conversation_id,
            message.id,
            user.pk,
            result.provider,
            result.model,
            result.status,
            result.latency_ms,
            result.usage.get("input_tokens", 0),
            result.usage.get("output_tokens", 0),
            result.request_id,
            result.error_code,
        )
        return SupportFlowResult(message, assistant)
    except Exception:
        SupportMessage.objects.filter(pk=message.pk).update(
            status=SupportMessage.Status.FAILED, error_code="internal_failure"
        )
        _release_request(token, user=user)
        raise


def create_ticket(
    *, conversation, user, description: str, include_question: bool = False,
    question_message=None, include_answer: bool = False, answer_message=None,
    include_screenshot: bool = False, include_diagnostics: bool = False,
    route_path: str = "", browser_family: str = "", viewport: str = "",
) -> DeveloperTicket:
    description = (description or "").strip()
    if not description:
        raise ValidationError("Опишите проблему.")
    snapshot = []
    attachment = None
    if include_question and question_message:
        question = conversation.messages.filter(
            pk=question_message, role=SupportMessage.Role.USER
        ).first()
        if question:
            snapshot.append({"message_id": str(question.id), "role": "user", "text": question.text})
            if include_screenshot:
                attachment = SupportAttachment.objects.filter(message=question).first()
    if include_answer and answer_message:
        answer = conversation.messages.filter(
            pk=answer_message, role=SupportMessage.Role.ASSISTANT
        ).first()
        if answer:
            snapshot.append(
                {"message_id": str(answer.id), "role": "assistant", "text": answer.text}
            )
    diagnostic = {}
    if include_diagnostics:
        diagnostic = safe_diagnostic_snapshot(
            user=user,
            route_context=safe_route_context(route_path),
            browser_family=browser_family,
            viewport=viewport,
        )
        diagnostic["captured_at"] = timezone.now().isoformat()
        diagnostic["conversation_id"] = str(conversation.id)
    with transaction.atomic():
        ticket = DeveloperTicket.objects.create(
            conversation=conversation,
            author=user,
            attachment=attachment,
            description=description[:4000],
            conversation_snapshot=snapshot,
            diagnostic_snapshot=diagnostic,
        )
        SupportConversation.objects.filter(pk=conversation.pk, owner=user).update(
            status=SupportConversation.Status.ESCALATED
        )
    return ticket
