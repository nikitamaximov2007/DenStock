"""MAX acceptance forensics. Read-only; prints facts, never message text or secrets.

Run inside web, naming the disposable acceptance requests by their 8-character
reference (and, if relevant, the employees who replied):

    docker compose exec -T -e MAX_FORENSICS_REFERENCES=4FEE829F,399B2E3A \\
        -e MAX_FORENSICS_OPERATORS=denis web python manage.py shell \\
        < scripts/operations/max_forensics.py > /root/max-forensics.txt

Every check prints ``PASS``, ``FAIL`` or ``INFO``; the last line is
``MAX FORENSICS PASS`` or ``MAX FORENSICS FAIL (n)``. ``business_counts=`` is the
same before and after acceptance: MAX creates no sale, reservation, movement,
receipt, repair, write-off, stock count or payment. Message bodies are never
printed; only counts, statuses, lengths and identities.
"""

import json
import os
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestMessengerLinkToken,
    MaxConversation,
    MaxCustomerChat,
    MaxDeliveryStatus,
    MaxMessage,
    MaxOperatorDelivery,
    MaxOutboxEvent,
)
from apps.operations.models import MaxBotRuntime, TelegramBotRuntime

OPEN_STATES = (MaxDeliveryStatus.PENDING, MaxDeliveryStatus.SENDING)
BAD_STATES = (MaxDeliveryStatus.FAILED, MaxDeliveryStatus.UNCERTAIN)


class Report:
    def __init__(self):
        self.failures = 0

    def line(self, status, text):
        if status == "FAIL":
            self.failures += 1
        print(f"{status} {text}")

    def check(self, condition, text):
        self.line("PASS" if condition else "FAIL", text)


def business_counts():
    from apps.counting.models import InventoryCountingSession
    from apps.customers.models import CustomerPeriodPaymentAcknowledgement
    from apps.inventory.models import StockMovement
    from apps.receipts.models import Receipt
    from apps.repairs.models import RepairIssueLine, RepairOrder
    from apps.sales.models import Reservation, ReservationLine, Sale, SaleLine
    from apps.stocktaking.models import InventoryCountDocument
    from apps.writeoffs.models import WriteOffDocument

    models = {
        "sales": Sale, "sale_lines": SaleLine, "reservations": Reservation,
        "reservation_lines": ReservationLine, "stock_movements": StockMovement,
        "receipts": Receipt, "repairs": RepairOrder, "repair_issues": RepairIssueLine,
        "writeoffs": WriteOffDocument, "stock_counts": InventoryCountDocument,
        "counting_sessions": InventoryCountingSession,
        "payments": CustomerPeriodPaymentAcknowledgement,
    }
    return {name: model.objects.count() for name, model in models.items()}


def check_request(report, request, operators):
    ref = request.reference
    report.check(request.preferred_messenger == "max", f"{ref} chose MAX")
    report.line("INFO", f"{ref} status={request.status}")
    tokens = CustomerRequestMessengerLinkToken.objects.filter(request=request, channel="max")
    used = tokens.filter(used_at__isnull=False).count()
    live = tokens.filter(used_at__isnull=True, revoked_at__isnull=True).count()
    issued = tokens.count()
    report.check(used == 1, f"{ref} exactly one link consumed (used={used}, issued={issued})")
    report.check(live == 0, f"{ref} no unused link left open (open={live})")
    conversation = MaxConversation.objects.filter(request=request).first()
    if conversation is None:
        report.line("FAIL", f"{ref} has no MAX conversation")
        return None
    report.check(conversation.is_linked, f"{ref} conversation linked")
    report.line("INFO", f"{ref} max_user={conversation.customer_user_id} "
                        f"dialog={conversation.customer_chat_id}")
    messages = MaxMessage.objects.filter(conversation=conversation)
    summary = messages.filter(dedupe_key__startswith="summary:")
    report.check(summary.exists() and not summary.exclude(delivery_status="sent").exists(),
                 f"{ref} start summary sent ({summary.count()} part(s))")
    inbound = messages.filter(direction=MaxMessage.Direction.CUSTOMER)
    mids = list(inbound.values_list("external_message_id", flat=True))
    report.check(len(mids) == len(set(mids)), f"{ref} inbound stored once per mid ({len(mids)})")
    report.line("INFO", f"{ref} mid lengths={sorted({len(mid) for mid in mids})}")
    acks = messages.filter(dedupe_key__startswith="ack:")
    report.check(acks.count() == (1 if mids else 0), f"{ref} acknowledgements={acks.count()}")
    report.check(not acks.exclude(delivery_status="sent").exists(), f"{ref} acknowledgement sent")
    replies = messages.filter(direction=MaxMessage.Direction.OPERATOR)
    report.check(
        not replies.exclude(delivery_status="sent").exists(),
        f"{ref} replies sent ({replies.count()})",
    )
    authors = sorted({str(user) for user in
                      replies.values_list("operator_user__username", flat=True)})
    report.check(all(authors) and not replies.filter(operator_user__isnull=True).exists(),
                 f"{ref} every reply names its DenisStock author {authors}")
    report.check(len(set(replies.values_list("external_message_id", flat=True))) ==
                 replies.count(), f"{ref} each reply delivered as its own MAX message")
    customer_facing = messages.exclude(direction=MaxMessage.Direction.CUSTOMER)
    leaked = [
        name for name in operators
        if name and customer_facing.exclude(direction=MaxMessage.Direction.OPERATOR)
        .filter(text__icontains=name).exists()
    ]
    report.check(not leaked, f"{ref} no employee identity in bot messages")
    events = MaxOutboxEvent.objects.filter(request=request)
    by_kind = dict(events.values_list("kind").annotate(n=Count("pk")))
    report.check(by_kind.get("customer_message", 0) == len(mids),
                 f"{ref} one operator event per customer message {by_kind}")
    report.check(by_kind.get("operator_reply", 0) == replies.count(),
                 f"{ref} one operator event per reply")
    report.check(not events.filter(status="pending").exists(), f"{ref} no pending operator event")
    deliveries = MaxOperatorDelivery.objects.filter(event__request=request)
    states = dict(deliveries.values_list("status").annotate(n=Count("pk")))
    report.check(not deliveries.filter(status__in=OPEN_STATES + BAD_STATES).exists(),
                 f"{ref} operator deliveries settled {states}")
    stuck = messages.exclude(direction=MaxMessage.Direction.CUSTOMER).filter(
        delivery_status__in=OPEN_STATES + BAD_STATES
    )
    report.check(not stuck.exists(), f"{ref} no pending/sending/failed/uncertain customer rows "
                                     f"({list(stuck.values_list('pk', 'delivery_status'))})")
    return conversation


def main():
    references = [
        item.strip().upper()
        for item in os.environ.get("MAX_FORENSICS_REFERENCES", "").split(",") if item.strip()
    ]
    operators = [
        item.strip() for item in os.environ.get("MAX_FORENSICS_OPERATORS", "").split(",")
        if item.strip()
    ]
    report = Report()
    print("# MAX acceptance forensics (read-only)")
    now = timezone.now()
    runtime = MaxBotRuntime.objects.filter(pk=MaxBotRuntime.SINGLETON_PK).first()
    report.check(
        runtime is not None and bool(runtime.worker_id) and runtime.lease_expires_at
        and runtime.lease_expires_at > now and runtime.heartbeat_at
        and now - runtime.heartbeat_at < timedelta(seconds=120),
        "max-bot lease held and heartbeat fresh",
    )
    telegram = TelegramBotRuntime.objects.filter(pk=TelegramBotRuntime.SINGLETON_PK).first()
    report.check(
        telegram is not None and telegram.heartbeat_at
        and now - telegram.heartbeat_at < timedelta(seconds=120),
        "telegram-bot heartbeat fresh",
    )
    duplicates = (
        MaxMessage.objects.filter(direction=MaxMessage.Direction.CUSTOMER)
        .values("external_message_id").annotate(n=Count("pk")).filter(n__gt=1).count()
    )
    report.check(duplicates == 0, "no inbound mid stored twice anywhere")
    conversations = []
    for reference in references:
        candidates = CustomerRequest.objects.filter(public_id__istartswith=reference.lower())
        matches = [r for r in candidates if r.reference == reference]
        if len(matches) != 1:
            report.line("FAIL", f"{reference} not found exactly once ({len(matches)})")
            continue
        conversation = check_request(report, matches[0], operators)
        if conversation is not None:
            conversations.append(conversation)
    users = {c.customer_user_id for c in conversations if c.customer_user_id}
    for user_id in sorted(users):
        chat = MaxCustomerChat.objects.filter(user_id=user_id).first()
        linked = MaxConversation.objects.filter(customer_user_id=user_id, status="linked")
        active = chat.active_conversation if chat else None
        active_ref = active.request.reference if active else None
        report.line(
            "INFO", f"max_user={user_id} linked_requests={linked.count()} active={active_ref}"
        )
    if len(conversations) >= 2:
        report.check(len(users) == 1, "returning customer used one MAX account for all requests")
    open_rows = MaxMessage.objects.exclude(direction=MaxMessage.Direction.CUSTOMER).filter(
        delivery_status__in=OPEN_STATES, next_attempt_at__lte=now
    ).count()
    report.check(open_rows == 0, f"MAX outbox has no due unsent rows ({open_rows})")
    report.line("INFO", "uncertain_or_failed_total=" + str(
        MaxMessage.objects.filter(delivery_status__in=BAD_STATES).count()
    ))
    print("business_counts=" + json.dumps(business_counts(), sort_keys=True))
    print("MAX FORENSICS PASS" if not report.failures
          else f"MAX FORENSICS FAIL ({report.failures})")


main()
