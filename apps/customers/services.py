"""Поиск, исторические снимки и ручные действия по карточке клиента."""

from __future__ import annotations

import datetime

from django.db import transaction
from django.db.models import DateTimeField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.core.phones import looks_like_phone, normalize_phone

from .models import Customer, CustomerPeriodPaymentAcknowledgement

SEARCH_LIMIT = 50
# Пол для клиентов без завершённых документов. Значение самой ранней возможной
# даты, а не NULL: NULL в GREATEST ведёт себя по-разному в PostgreSQL и SQLite,
# и порядок «без истории - вниз» перестал бы быть одинаковым в проде и тестах.
_NO_ACTIVITY = datetime.datetime(1, 1, 1, tzinfo=datetime.UTC)


def search_customers(query: str, *, limit: int = SEARCH_LIMIT):
    """Клиенты по имени (подстрока) или телефону в любом привычном формате."""
    query = (query or "").strip()
    queryset = Customer.objects.all()
    if not query:
        return queryset[:limit]
    condition = Q(name__icontains=query)
    if looks_like_phone(query):
        normalized = normalize_phone(query)
        if normalized:
            condition |= Q(phone_normalized__contains=normalized)
    return queryset.filter(condition)[:limit]


def customers_by_recent_activity(*, limit: int | None = SEARCH_LIMIT):
    """Клиенты по свежести последнего ЗАВЕРШЁННОГО документа, свежие - первыми.

    Сотрудник почти всегда оформляет документ на того, кто уже приходил, и
    искал его до сих пор в алфавитном списке на несколько сотен строк. Порядок
    следует за работой: первым идёт клиент с самой свежей завершённой продажей
    или выдачей в ремонт.

    Отменённые и черновые документы клиента наверх не поднимают: товар к нему
    не уходил. Клиенты без истории остаются ниже всех, между собой - по имени.
    Имя и первичный ключ дают устойчивый порядок при одинаковых датах, иначе
    список менялся бы от запроса к запросу.
    """
    from apps.repairs.models import RepairOrder
    from apps.sales.models import Sale

    last_sale = (
        Sale.objects.filter(customer=OuterRef("pk"), status=Sale.Status.COMPLETED)
        .order_by("-sold_at")
        .values("sold_at")[:1]
    )
    last_repair = (
        RepairOrder.objects.filter(
            customer=OuterRef("pk"), status=RepairOrder.Status.COMPLETED
        )
        .order_by("-completed_at")
        .values("completed_at")[:1]
    )
    floor = Value(_NO_ACTIVITY, output_field=DateTimeField())
    ordered = (
        Customer.objects.annotate(
            last_sale_at=Subquery(last_sale, output_field=DateTimeField()),
            last_repair_at=Subquery(last_repair, output_field=DateTimeField()),
        )
        .annotate(
            last_activity_at=Greatest(
                Coalesce("last_sale_at", floor),
                Coalesce("last_repair_at", floor),
                output_field=DateTimeField(),
            )
        )
        .order_by("-last_activity_at", "name", "pk")
    )
    # ModelChoiceField проверяет выбранное значение запросом по ключу, а
    # срезанный queryset этого не позволяет: там ограничение снимает limit=None.
    return ordered[:limit] if limit is not None else ordered


def customer_snapshot(customer, *, fallback_name="", fallback_phone="") -> dict:
    """Return the document snapshot for a linked customer or legacy free text."""
    if customer is not None:
        return customer.snapshot()
    return {
        "customer_name": (fallback_name or "").strip(),
        "customer_phone": (fallback_phone or "").strip(),
    }


def documents_of(customer):
    """Document counters used by the customer card and deletion safeguard."""
    from apps.repairs.models import RepairOrder
    from apps.sales.models import Reservation, Sale

    return {
        "sales": Sale.objects.filter(customer=customer).count(),
        "repairs": RepairOrder.objects.filter(customer=customer).count(),
        "reservations": Reservation.objects.filter(customer=customer).count(),
    }


class PaymentAcknowledgementError(ValueError):
    """Подтверждение нельзя создать для неполной денежной картины."""


@transaction.atomic
def acknowledge_customer_period_payment(*, customer_id, period, by):
    """Сохранить подтверждение для пересчитанной сервером суммы периода.

    Повторный POST без изменения начислений идемпотентен. Старые снимки не
    переписываются: при новом подтверждении они закрываются как заменённые.
    """
    from apps.reports.payment_status import customer_payment_state

    customer = Customer.objects.select_for_update().get(pk=customer_id)
    state = customer_payment_state(customer_id=customer.pk, period=period)
    if not state["acknowledgeable"]:
        raise PaymentAcknowledgementError("Нельзя подтвердить оплату: сумма клиента не определена.")

    active = list(
        CustomerPeriodPaymentAcknowledgement.objects.select_for_update()
        .filter(
            customer=customer,
            period_start=period.date_from,
            period_end=period.date_to,
            revoked_at__isnull=True,
        )
        .order_by("-acknowledged_at", "-pk")
    )
    if active and (
        active[0].amount_rub == state["amount"]
        and active[0].billable_fingerprint == state["fingerprint"]
    ):
        return active[0]

    if active:
        CustomerPeriodPaymentAcknowledgement.objects.filter(
            pk__in=[row.pk for row in active]
        ).update(revoked_at=timezone.now())
    return CustomerPeriodPaymentAcknowledgement.objects.create(
        customer=customer,
        period_start=period.date_from,
        period_end=period.date_to,
        amount_rub=state["amount"],
        billable_fingerprint=state["fingerprint"],
        document_count=state["document_count"],
        acknowledged_by=by,
    )


@transaction.atomic
def revoke_customer_period_payment(*, customer_id, period, by) -> int:
    """Снять ручное подтверждение, оставив его исторический снимок в журнале."""
    customer = Customer.objects.select_for_update().get(pk=customer_id)
    return CustomerPeriodPaymentAcknowledgement.objects.filter(
        customer=customer,
        period_start=period.date_from,
        period_end=period.date_to,
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now(), revoked_by=by)
