"""Shared customer-cabinet rules for Telegram and MAX.

The messenger provider identity is the authentication boundary.  A purchase
is visible only when that identity is linked to an active DenisStock Customer
card.  No browser session, name, username or phone heuristic is consulted.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from apps.catalog.models import PartType
from apps.catalog.public_contracts import resolve_current_customer_prices
from apps.customer_accounts.models import CustomerIdentity, Provider
from apps.customers.models import Customer
from apps.inventory.availability import available_totals
from apps.inventory.presentation import part_exact_number
from apps.sales.models import Sale

from .models import CustomerRequest
from .policies import PUBLIC_REQUEST_CONSENT_PURPOSE, current_consent_versions
from .services import RequestLineInput, create_customer_request

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class PurchaseLine:
    part_id: int
    name: str
    article: str
    quantity: Decimal
    unit_price: Decimal
    total_price: Decimal


@dataclass(frozen=True, slots=True)
class CustomerPurchase:
    sale_id: int
    number: str
    sold_at: object
    total: Decimal
    lines: tuple[PurchaseLine, ...]


@dataclass(frozen=True, slots=True)
class ReorderLine:
    part_id: int
    name: str
    article: str
    historical_quantity: Decimal
    requested_quantity: Decimal
    historical_unit_price: Decimal
    current_unit_price: Decimal | None
    available_quantity: Decimal
    available: bool
    supply_inquiry: bool
    reason: str = ""

    @property
    def current_total(self) -> Decimal | None:
        if self.current_unit_price is None:
            return None
        return self.current_unit_price * self.requested_quantity


@dataclass(frozen=True, slots=True)
class ReorderPreview:
    purchase: CustomerPurchase
    lines: tuple[ReorderLine, ...]

    @property
    def available_lines(self) -> tuple[ReorderLine, ...]:
        return tuple(line for line in self.lines if line.available)

    @property
    def total(self) -> Decimal | None:
        values = [line.current_total for line in self.available_lines]
        return sum(values, ZERO) if values and all(value is not None for value in values) else None


class CabinetAccessError(ValueError):
    """A safe fail-closed customer-facing cabinet error."""


def _provider(value: str) -> str:
    if value not in Provider.values:
        raise CabinetAccessError("Неизвестный мессенджер.")
    return value


def linked_customer(*, provider: str, provider_user_id: int) -> Customer | None:
    """Return only the explicitly linked DenisStock Customer, if any."""
    provider = _provider(provider)
    if not isinstance(provider_user_id, int) or isinstance(provider_user_id, bool):
        return None
    identity = (
        CustomerIdentity.objects.select_related("account")
        .filter(provider=provider, provider_user_id=provider_user_id)
        .first()
    )
    if identity is None or not identity.account.is_active:
        return None
    from apps.customer_accounts.models import CustomerAccountCustomerLink

    link = (
        CustomerAccountCustomerLink.objects.select_related("customer")
        .filter(account=identity.account, unlinked_at__isnull=True)
        .first()
    )
    return link.customer if link is not None else None


def _purchase_queryset(*, provider: str, provider_user_id: int):
    customer = linked_customer(provider=provider, provider_user_id=provider_user_id)
    if customer is None:
        return Customer.objects.none(), None
    return (
        Sale.objects.filter(customer=customer, status=Sale.Status.COMPLETED)
        .prefetch_related("lines__part_type__numbers")
        .order_by("-sold_at", "-pk"),
        customer,
    )


def _purchase_dto(sale: Sale) -> CustomerPurchase:
    lines = tuple(
        PurchaseLine(
            part_id=line.part_type_id,
            name=line.part_type.name,
            article=part_exact_number(line.part_type, default=""),
            quantity=line.quantity,
            unit_price=line.unit_price,
            total_price=line.total_price,
        )
        for line in sale.lines.all()
    )
    return CustomerPurchase(
        sale_id=sale.pk,
        number=sale.number,
        sold_at=sale.sold_at or sale.created_at,
        total=sale.revenue_total,
        lines=lines,
    )


def list_customer_purchases(
    *, provider: str, provider_user_id: int
) -> tuple[CustomerPurchase, ...]:
    sales, _customer = _purchase_queryset(provider=provider, provider_user_id=provider_user_id)
    return tuple(_purchase_dto(sale) for sale in sales)


def get_customer_purchase(
    *, provider: str, provider_user_id: int, sale_id: int
) -> CustomerPurchase | None:
    sales, _customer = _purchase_queryset(provider=provider, provider_user_id=provider_user_id)
    sale = sales.filter(pk=sale_id).first()
    return _purchase_dto(sale) if sale is not None else None


def build_reorder_preview(
    *, provider: str, provider_user_id: int, sale_id: int
) -> ReorderPreview | None:
    """Build a current, read-only preview from an owned historical sale."""
    sales, _customer = _purchase_queryset(provider=provider, provider_user_id=provider_user_id)
    sale = sales.filter(pk=sale_id).first()
    if sale is None:
        return None
    purchase = _purchase_dto(sale)
    historical = {line.part_id: line for line in purchase.lines}
    part_ids = list(historical)
    parts = {
        part.pk: part
        for part in PartType.objects.filter(pk__in=part_ids).prefetch_related("numbers")
    }
    quantities = available_totals(part_ids)
    prices = resolve_current_customer_prices(parts.values())
    result = []
    for historical_line in purchase.lines:
        part = parts.get(historical_line.part_id)
        if part is None or not part.is_active or not part.is_public:
            result.append(
                ReorderLine(
                    part_id=historical_line.part_id,
                    name=historical_line.name,
                    article=historical_line.article,
                    historical_quantity=historical_line.quantity,
                    requested_quantity=ZERO,
                    historical_unit_price=historical_line.unit_price,
                    current_unit_price=None,
                    available_quantity=ZERO,
                    available=False,
                    supply_inquiry=False,
                    reason="Деталь больше недоступна в каталоге.",
                )
            )
            continue
        available = quantities.get(part.pk, ZERO)
        requested = min(historical_line.quantity, available) if available > ZERO else ZERO
        price = prices[part.pk].price_rub
        result.append(
            ReorderLine(
                part_id=part.pk,
                name=part.name,
                article=part_exact_number(part, default=""),
                historical_quantity=historical_line.quantity,
                requested_quantity=requested,
                historical_unit_price=historical_line.unit_price,
                current_unit_price=price,
                available_quantity=available,
                available=True,
                supply_inquiry=available <= ZERO,
                reason="Нет в наличии." if available <= ZERO else "",
            )
        )
    return ReorderPreview(purchase=purchase, lines=tuple(result))


@transaction.atomic
def create_request_from_reorder_preview(
    *, provider: str, provider_user_id: int, sale_id: int, submission_key: str
) -> tuple[CustomerRequest, bool]:
    """Rebuild and revalidate the preview, then create a new request only."""
    provider = _provider(provider)
    customer = linked_customer(provider=provider, provider_user_id=provider_user_id)
    preview = build_reorder_preview(
        provider=provider, provider_user_id=provider_user_id, sale_id=sale_id
    )
    if customer is None or preview is None or not preview.available_lines:
        raise CabinetAccessError("Покупка недоступна для повторного заказа.")
    lines = [
        RequestLineInput(
            part_id=line.part_id,
            quantity=line.requested_quantity,
            supply_inquiry=line.supply_inquiry,
        )
        for line in preview.available_lines
        if line.supply_inquiry or line.requested_quantity > ZERO
    ]
    if not lines:
        raise CabinetAccessError("Сейчас нет доступных позиций для заявки.")
    privacy, consent = current_consent_versions()
    request, created = create_customer_request(
        customer_name=customer.name,
        customer_phone=customer.phone,
        preferred_messenger=provider,
        comment=f"Повторная заявка по покупке №{preview.purchase.number}",
        lines=lines,
        privacy_policy_version=privacy,
        personal_data_consent_version=consent,
        submission_key=submission_key,
        consent_purpose=PUBLIC_REQUEST_CONSENT_PURPOSE,
    )
    if created:
        # Reorder requests belong to the authenticated messenger identity from
        # the moment of confirmation, without using the dormant web session.
        link_key = submission_key[-48:]
        if provider == Provider.TELEGRAM:
            from .telegram_service import bind_customer_chat

            bind_customer_chat(
                request=request,
                chat_id=provider_user_id,
                user_id=provider_user_id,
                username="",
                link_token_id=link_key,
            )
        else:
            from .max_service import bind_customer_chat

            bind_customer_chat(
                request=request,
                chat_id=provider_user_id,
                user_id=provider_user_id,
                link_token_id=link_key,
            )
    return request, created
