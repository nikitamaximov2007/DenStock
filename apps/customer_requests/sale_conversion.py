"""Safe conversion of one customer request into one completed sale.

The request remains an immutable customer-facing snapshot.  This module only
links it to a customer, prepares an ordinary Sale draft, and performs the
final current-price/stock checks immediately before the existing sale service
does the physical stock movement.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from apps.catalog.public_contracts import resolve_current_customer_prices
from apps.core.phones import canonical_phone_text, normalize_phone
from apps.customers.models import Customer
from apps.inventory.models import PartItem, StockLot
from apps.sales.models import Sale
from apps.sales.services import (
    SaleError,
    active_reserved_for_lot,
    add_part_item_to_sale,
    add_stock_lot_to_sale,
    complete_sale,
    create_sale,
)

from .models import CustomerRequest


class CustomerRequestSaleError(SaleError):
    """A request cannot be safely prepared or completed as a sale."""


@dataclass(frozen=True, slots=True)
class CustomerMatch:
    normalized_phone: str
    customers: tuple[Customer, ...]

    @property
    def count(self) -> int:
        return len(self.customers)


def _legacy_phone_matches(normalized_phone: str) -> list[Customer]:
    """Find old cards whose stored phone predates the indexed helper field."""
    if not normalized_phone:
        return []
    matches = []
    for customer in Customer.objects.filter(phone_normalized="").exclude(phone="").only(
        "pk", "name", "phone", "phone_normalized"
    ):
        if normalize_phone(customer.phone) == normalized_phone:
            matches.append(customer)
    return matches


def match_request_customer(customer_request: CustomerRequest) -> CustomerMatch:
    """Resolve by exact normalized phone, including legacy raw phone values."""
    normalized = normalize_phone(customer_request.customer_phone)
    indexed = list(
        Customer.objects.filter(phone_normalized=normalized).order_by("pk")
        if normalized
        else Customer.objects.none()
    )
    # Verify the raw value even for indexed rows: the raw phone is the
    # authoritative historical value and a stale denormalized field must not
    # create a false match.
    matches = [
        customer for customer in indexed if normalize_phone(customer.phone) == normalized
    ]
    matches.extend(_legacy_phone_matches(normalized))
    matches.sort(key=lambda customer: customer.pk)
    return CustomerMatch(normalized_phone=normalized, customers=tuple(matches))


def _customer_from_selection(
    request: CustomerRequest, match: CustomerMatch, *, customer_id=None, create_customer=False
) -> Customer:
    if request.customer_id:
        return request.customer
    if match.count > 1:
        try:
            selected = int(customer_id)
        except (TypeError, ValueError) as exc:
            raise CustomerRequestSaleError(
                "Найдено несколько клиентов с этим номером. Выберите клиента."
            ) from exc
        for customer in match.customers:
            if customer.pk == selected:
                return customer
        raise CustomerRequestSaleError("Выбранный клиент не найден среди совпадений.")
    if match.count == 1:
        return match.customers[0]
    if not create_customer:
        raise CustomerRequestSaleError(
            "Клиент по телефону не найден. Выберите «Создать клиента из заявки»."
        )
    if not request.customer_name.strip() or not match.normalized_phone:
        raise CustomerRequestSaleError("Для создания клиента нужны имя и телефон.")
    return Customer.objects.create(
        name=request.customer_name,
        phone=canonical_phone_text(request.customer_phone),
    )


def _add_request_stock_lines(sale: Sale, request: CustomerRequest, *, by=None) -> None:
    lines = list(request.lines.select_related("part_type", "part_type__unit").order_by("pk"))
    if not lines:
        raise CustomerRequestSaleError("В заявке нет позиций для продажи.")
    prices = resolve_current_customer_prices({line.part_type for line in lines})
    for request_line in lines:
        price = prices[request_line.part_type_id].price_rub
        if price is None or price <= 0:
            raise CustomerRequestSaleError(
                f"{request_line.part_name}: текущая цена не позволяет провести продажу."
            )
        if request_line.is_supply_inquiry:
            raise CustomerRequestSaleError(
                f"{request_line.part_name}: это запрос о поставке, а не продажная позиция."
            )
        if request_line.part_type.is_oil:
            # V1: публичная заявка на масло - пакетная (quantity_requested = число
            # упаковок), а не литры. Автоматически перенести её в строку продажи
            # здесь означало бы взять это число как ЛИТРЫ по цене УПАКОВКИ за
            # литр - и неверный объём, и цена в разы завышена. Позицию
            # пропускаем: оператор добавляет масло в уже подготовленный
            # черновик вручную (раздел «Масло» на карточке продажи), указывая
            # реальный отпускаемый объём. См. _validate_request_sale_lines и
            # complete_request_sale - обе явно знают про этот пропуск.
            continue

        remaining = request_line.quantity_requested
        item_ids = list(
            PartItem.objects.filter(
                part_type_id=request_line.part_type_id,
                status=PartItem.Status.AVAILABLE,
            ).order_by("pk").values_list("pk", flat=True)
        )
        for item_id in item_ids:
            if remaining < 1:
                break
            try:
                add_part_item_to_sale(
                    sale, PartItem.objects.get(pk=item_id), unit_price=price, by=by
                )
            except SaleError:
                # A reservation or a concurrent status change removes this
                # candidate; the final sale check will still be authoritative.
                continue
            remaining -= Decimal("1")

        if remaining > 0:
            lots = StockLot.objects.filter(
                part_type_id=request_line.part_type_id,
                status=StockLot.Status.AVAILABLE,
                quantity__gt=0,
            ).order_by("pk")
            for lot in lots:
                available = lot.quantity - active_reserved_for_lot(lot)
                if available <= 0:
                    continue
                quantity = min(remaining, available)
                try:
                    add_stock_lot_to_sale(sale, lot, quantity, unit_price=price, by=by)
                except SaleError:
                    continue
                remaining -= quantity
                if remaining <= 0:
                    break
        if remaining > 0:
            raise CustomerRequestSaleError(
                f"{request_line.part_name}: недостаточно доступного остатка."
            )


@transaction.atomic
def prepare_request_sale(
    *, request_id: int, by=None, customer_id=None, create_customer: bool = False
) -> Sale:
    """Create or return the one linked draft, without any stock movement."""
    request = (
        CustomerRequest.objects.select_for_update(of=("self",))
        .select_related("customer", "sale")
        .get(pk=request_id)
    )
    if request.sale_id:
        return Sale.objects.get(pk=request.sale_id)
    if request.status != CustomerRequest.Status.IN_PROGRESS:
        raise CustomerRequestSaleError("Сначала возьмите заявку в работу.")
    match = match_request_customer(request)
    customer = _customer_from_selection(
        request, match, customer_id=customer_id, create_customer=create_customer
    )
    sale = create_sale(
        customer=customer,
        comment=f"По заявке №{request.reference}",
        by=by,
    )
    _add_request_stock_lines(sale, request, by=by)
    request.customer = customer
    request.sale = sale
    request.save(update_fields=["customer", "sale", "updated_at"])
    return sale


def _validate_request_sale_lines(request: CustomerRequest, sale: Sale) -> dict[int, Decimal]:
    request_lines = [line for line in request.lines.all() if not line.is_supply_inquiry]
    # Масло: заявка считает упаковками, строка продажи - литрами (её вручную
    # добавляет оператор через apps.sales.services.add_oil_volume_to_sale) -
    # эти числа НИКОГДА не совпадут по построению, поэтому для масла
    # проверяется только «позиция добавлена», а не точное количество.
    expected = {
        line.part_type_id: line.quantity_requested
        for line in request_lines
        if not line.part_type.is_oil
    }
    expected_oil_ids = {line.part_type_id for line in request_lines if line.part_type.is_oil}
    actual = {}
    actual_oil_ids = set()
    for line in sale.lines.select_related("part_type"):
        if line.part_type.is_oil:
            actual_oil_ids.add(line.part_type_id)
            continue
        actual[line.part_type_id] = actual.get(line.part_type_id, Decimal("0")) + line.quantity
    if actual != expected or not expected_oil_ids <= actual_oil_ids:
        raise CustomerRequestSaleError(
            "Состав черновика изменён. Сверьте позиции заявки перед проведением."
        )
    return expected


@transaction.atomic
def complete_request_sale(*, request_id: int, sale_id: int, by=None) -> Sale:
    """Recheck customer, current prices and stock, then complete atomically."""
    request = (
        CustomerRequest.objects.select_for_update(of=("self",))
        .select_related("customer")
        .get(pk=request_id)
    )
    sale = Sale.objects.select_for_update().get(pk=sale_id)
    if request.sale_id != sale.pk:
        raise CustomerRequestSaleError("Продажа не связана с этой заявкой.")
    if sale.status == Sale.Status.COMPLETED:
        return sale
    if request.status == CustomerRequest.Status.COMPLETED:
        raise CustomerRequestSaleError("Заявка уже выполнена без проведённой продажи.")
    if request.customer_id is None or sale.customer_id != request.customer_id:
        raise CustomerRequestSaleError("Сначала подтвердите карточку клиента.")

    _validate_request_sale_lines(request, sale)
    part_types = {
        line.part_type for line in sale.lines.select_related("part_type")
        if not line.part_type.is_oil
    }
    prices = resolve_current_customer_prices(part_types)
    for line in sale.lines.select_for_update().select_related("part_type"):
        if line.part_type.is_oil:
            # Цена/сумма уже верно посчитаны и заморожены при ручном
            # добавлении (add_oil_volume_to_sale) - price здесь была бы
            # ценой УПАКОВКИ, а не за литр, и испортила бы строку.
            continue
        price = prices[line.part_type_id].price_rub
        if price is None or price <= 0:
            raise CustomerRequestSaleError(
                f"{line.part_type}: текущая цена больше не позволяет провести продажу."
            )
        line.unit_price = price
        line.total_price = price * line.quantity
        line.save(update_fields=["unit_price", "total_price"])

    try:
        sale = complete_sale(sale, by=by)
    except SaleError as exc:
        raise CustomerRequestSaleError(str(exc)) from exc
    request.status = CustomerRequest.Status.COMPLETED
    request.save(update_fields=["status", "updated_at"])
    return sale
