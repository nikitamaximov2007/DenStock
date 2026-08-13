"""Поиск клиентов и подстановка снимка клиента в документ.

Здесь нет складской логики: карточка клиента ничего не резервирует и не
списывает. Единственное правило, которое обязана держать эта прослойка, это
историческая корректность документов: документ получает СНИМОК имени и
телефона в момент создания и больше от карточки не зависит.
"""

from __future__ import annotations

from django.db.models import Q

from apps.core.phones import looks_like_phone, normalize_phone

from .models import Customer

SEARCH_LIMIT = 50


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


def customer_snapshot(customer, *, fallback_name="", fallback_phone="") -> dict:
    """Снимок клиента для нового документа.

    Выбран клиент из справочника - берём его актуальные значения. Клиента нет
    (legacy-поток со свободным вводом) - остаются введённые вручную значения.
    """
    if customer is not None:
        return customer.snapshot()
    return {
        "customer_name": (fallback_name or "").strip(),
        "customer_phone": (fallback_phone or "").strip(),
    }


def documents_of(customer):
    """Счётчики документов клиента: нужны карточке и защите от удаления."""
    from apps.repairs.models import RepairOrder
    from apps.sales.models import Reservation, Sale

    return {
        "sales": Sale.objects.filter(customer=customer).count(),
        "repairs": RepairOrder.objects.filter(customer=customer).count(),
        "reservations": Reservation.objects.filter(customer=customer).count(),
    }
