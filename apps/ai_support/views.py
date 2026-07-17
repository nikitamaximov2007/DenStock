import uuid

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts import roles
from apps.accounts.permissions import capability_required

from .files import private_path
from .forms import MessageForm, RatingForm, TicketForm, TicketStatusForm
from .models import (
    DeveloperTicket,
    SupportAttachment,
    SupportConversation,
    SupportMessage,
    SupportRating,
)
from .services import (
    ConcurrentRequest,
    FeatureDisabled,
    ImageRejected,
    ProviderCapacity,
    QuotaExceeded,
    create_ticket,
    send_message,
)


def _provider_state():
    if not settings.AI_SUPPORT_ENABLED:
        return "disabled"
    if settings.AI_SUPPORT_PROVIDER == "disabled":
        return "unavailable"
    if settings.AI_SUPPORT_PROVIDER == "codex_cli" and (
        not settings.AI_SUPPORT_CODEX_MODEL
        or not str(settings.AI_SUPPORT_CODEX_HOME)
        or not str(settings.AI_SUPPORT_CODEX_WORKSPACE)
    ):
        return "unavailable"
    return "ready"


def _conversation_context(request, conversation=None, *, form=None):
    conversations = SupportConversation.objects.filter(owner=request.user)[:30]
    messages_qs = []
    latest_question = None
    latest_answer = None
    if conversation:
        messages_qs = conversation.messages.select_related("rating").order_by("sequence")
        latest_question = conversation.messages.filter(role=SupportMessage.Role.USER).last()
        latest_answer = conversation.messages.filter(role=SupportMessage.Role.ASSISTANT).last()
    return {
        "conversation": conversation,
        "conversations": conversations,
        "support_messages": messages_qs,
        "message_form": form or MessageForm(initial={"idempotency_token": uuid.uuid4()}),
        "provider_state": _provider_state(),
        "max_message_chars": settings.AI_SUPPORT_MAX_MESSAGE_CHARS,
        "latest_question": latest_question,
        "latest_answer": latest_answer,
        "quick_questions": (
            "Не получается провести продажу",
            "Почему не совпадают остатки?",
            "Как принять новую деталь?",
            "Как отменить ошибочную продажу?",
            "Где посмотреть историю действий?",
            "Что означает эта ошибка?",
        ),
        "can_manage_tickets": request.user.has_capability(roles.MANAGE_AI_SUPPORT_TICKETS),
    }


@require_GET
@capability_required(roles.USE_AI_SUPPORT)
def support_home(request):
    conversation = SupportConversation.objects.filter(owner=request.user).first()
    if conversation:
        return redirect("ai_support:conversation", conversation_id=conversation.id)
    return render(request, "ai_support/index.html", _conversation_context(request))


@require_POST
@capability_required(roles.USE_AI_SUPPORT)
def conversation_create(request):
    conversation = SupportConversation.objects.create(owner=request.user)
    return redirect("ai_support:conversation", conversation_id=conversation.id)


@require_GET
@capability_required(roles.USE_AI_SUPPORT)
def conversation_detail(request, conversation_id):
    conversation = get_object_or_404(
        SupportConversation, pk=conversation_id, owner=request.user
    )
    return render(
        request,
        "ai_support/index.html",
        _conversation_context(request, conversation),
    )


@require_POST
@capability_required(roles.USE_AI_SUPPORT)
def message_send(request, conversation_id):
    conversation = get_object_or_404(
        SupportConversation, pk=conversation_id, owner=request.user
    )
    form = MessageForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(
            request,
            "ai_support/index.html",
            _conversation_context(request, conversation, form=form),
            status=400,
        )
    try:
        result = send_message(
            conversation=conversation,
            user=request.user,
            text=form.cleaned_data["text"],
            token=form.cleaned_data["idempotency_token"],
            route_path=form.cleaned_data.get("route_path", ""),
            upload=form.cleaned_data.get("image"),
            image_consent=form.cleaned_data.get("image_consent", False),
        )
    except FeatureDisabled:
        messages.warning(request, "ИИ-поддержка выключена. Создайте ручное обращение.")
    except QuotaExceeded:
        messages.warning(request, "Лимит запросов исчерпан. Создайте ручное обращение.")
    except ConcurrentRequest:
        messages.warning(request, "Предыдущий запрос ещё обрабатывается. Подождите его завершения.")
    except ProviderCapacity:
        messages.warning(request, "ИИ-поддержка сейчас занята. Попробуйте немного позже.")
    except ImageRejected:
        messages.error(request, "Изображение отклонено безопасной проверкой.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        if result.duplicate:
            messages.info(request, "Повторный запрос не отправлен провайдеру.")
    return redirect("ai_support:conversation", conversation_id=conversation.id)


@require_POST
@capability_required(roles.USE_AI_SUPPORT)
def message_rating(request, message_id):
    assistant = get_object_or_404(
        SupportMessage,
        pk=message_id,
        role=SupportMessage.Role.ASSISTANT,
        conversation__owner=request.user,
    )
    form = RatingForm(request.POST)
    if form.is_valid():
        SupportRating.objects.update_or_create(
            assistant_message=assistant,
            defaults={
                "user": request.user,
                "value": form.cleaned_data["value"],
                "comment": form.cleaned_data["comment"],
            },
        )
        messages.success(request, "Оценка сохранена.")
    else:
        messages.error(request, "Не удалось сохранить оценку.")
    return redirect("ai_support:conversation", conversation_id=assistant.conversation_id)


@require_POST
@capability_required(roles.USE_AI_SUPPORT)
def ticket_create(request, conversation_id):
    conversation = get_object_or_404(
        SupportConversation, pk=conversation_id, owner=request.user
    )
    form = TicketForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Проверьте описание и выбранные данные обращения.")
        return redirect("ai_support:conversation", conversation_id=conversation.id)
    try:
        ticket = create_ticket(
            conversation=conversation,
            user=request.user,
            description=form.cleaned_data["description"],
            include_question=form.cleaned_data.get("include_question", False),
            question_message=form.cleaned_data.get("question_message"),
            include_answer=form.cleaned_data.get("include_answer", False),
            answer_message=form.cleaned_data.get("answer_message"),
            include_screenshot=form.cleaned_data.get("include_screenshot", False),
            include_diagnostics=form.cleaned_data.get("include_diagnostics", False),
            route_path=form.cleaned_data.get("route_path", ""),
            browser_family=form.cleaned_data.get("browser_family", ""),
            viewport=form.cleaned_data.get("viewport", ""),
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, f"Обращение {ticket.id} создано.")
    return redirect("ai_support:conversation", conversation_id=conversation.id)


@require_GET
@capability_required(roles.USE_AI_SUPPORT)
def attachment_download(request, attachment_id):
    attachment = get_object_or_404(
        SupportAttachment.objects.select_related("message__conversation"), pk=attachment_id
    )
    owns = attachment.message.conversation.owner_id == request.user.id
    manages_shared = request.user.has_capability(
        roles.MANAGE_AI_SUPPORT_TICKETS
    ) and attachment.tickets.exists()
    if not owns and not manages_shared:
        raise Http404
    try:
        path = private_path(attachment.relative_path)
        handle = path.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404 from exc
    response = FileResponse(
        handle,
        as_attachment=False,
        filename=f"{attachment.id}{path.suffix}",
        content_type=attachment.mime_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
@capability_required(roles.MANAGE_AI_SUPPORT_TICKETS)
def ticket_list(request):
    tickets = DeveloperTicket.objects.select_related("author").all()[:100]
    return render(request, "ai_support/ticket_list.html", {"tickets": tickets})


@require_GET
@capability_required(roles.MANAGE_AI_SUPPORT_TICKETS)
def ticket_detail(request, ticket_id):
    ticket = get_object_or_404(DeveloperTicket.objects.select_related("author"), pk=ticket_id)
    return render(request, "ai_support/ticket_detail.html", {"ticket": ticket})


@require_POST
@capability_required(roles.MANAGE_AI_SUPPORT_TICKETS)
def ticket_status(request, ticket_id):
    ticket = get_object_or_404(DeveloperTicket, pk=ticket_id)
    form = TicketStatusForm(request.POST)
    if form.is_valid():
        ticket.status = form.cleaned_data["status"]
        ticket.save(update_fields=["status", "updated_at"])
        messages.success(request, "Статус обращения обновлён.")
    return redirect("ai_support:ticket_detail", ticket_id=ticket.id)
