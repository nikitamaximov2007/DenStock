from decimal import Decimal

import pytest
from django.utils import timezone

from apps.customer_accounts.models import (
    CustomerAccount,
    CustomerAccountCustomerLink,
    CustomerIdentity,
    Provider,
)
from apps.customer_requests import customer_ui, max_service, telegram_service
from apps.customer_requests.customer_cabinet import (
    build_reorder_preview,
    create_request_from_reorder_preview,
    get_customer_purchase,
    list_customer_purchases,
)
from apps.customer_requests.models import MaxConversation, TelegramConversation
from tests.customer_account_support import make_customer, make_sale


def _identity(customer, user_id, *, provider=Provider.MAX, admin):
    account = CustomerAccount.objects.create(display_name=customer.name)
    CustomerIdentity.objects.create(
        account=account,
        provider=provider,
        provider_user_id=user_id,
        verified_at=timezone.now(),
    )
    CustomerAccountCustomerLink.objects.create(account=account, customer=customer, linked_by=admin)
    return account


@pytest.mark.django_db
def test_purchases_are_sale_history_and_fail_closed_for_foreign_identity(public_catalog):
    customer = make_customer("Алиса")
    other = make_customer("Боб")
    part = public_catalog.part("Топливный фильтр", article="10F", price="1500")
    lot = public_catalog.stock(part, "10")
    sale = make_sale(customer, part, lot=lot, quantity="2", unit_price="1200")
    _identity(customer, 1001, admin=public_catalog.user)
    _identity(other, 1002, admin=public_catalog.user)

    purchases = list_customer_purchases(provider=Provider.MAX, provider_user_id=1001)
    assert [item.sale_id for item in purchases] == [sale.pk]
    assert purchases[0].lines[0].unit_price == Decimal("1200")
    assert not hasattr(purchases[0], "cost_total")
    detail = customer_ui.purchase_detail_text(purchases[0])
    assert "6161" not in detail and "7171" not in detail and "8181" not in detail
    assert list_customer_purchases(provider=Provider.MAX, provider_user_id=1002) == ()
    assert (
        get_customer_purchase(provider=Provider.MAX, provider_user_id=1002, sale_id=sale.pk)
        is None
    )
    assert (
        get_customer_purchase(provider=Provider.MAX, provider_user_id=1001, sale_id=sale.pk).number
        == sale.number
    )


@pytest.mark.django_db
def test_request_without_sale_is_not_purchase(public_catalog):
    customer = make_customer("Алиса")
    _identity(customer, 2001, admin=public_catalog.user)
    assert list_customer_purchases(provider=Provider.TELEGRAM, provider_user_id=2001) == ()


@pytest.mark.django_db
def test_reorder_uses_current_price_and_caps_stock(public_catalog):
    customer = make_customer("Алиса",)
    part = public_catalog.part("Фильтр", article="A-1", price="1500")
    lot = public_catalog.stock(part, "1")
    sale = make_sale(customer, part, lot=lot, quantity="2", unit_price="1200")
    _identity(customer, 3001, admin=public_catalog.user)

    preview = build_reorder_preview(provider=Provider.MAX, provider_user_id=3001, sale_id=sale.pk)
    line = preview.lines[0]
    assert line.historical_unit_price == Decimal("1200")
    assert line.current_unit_price == Decimal("1500")
    assert line.requested_quantity == Decimal("1")
    assert line.available_quantity == Decimal("1")


@pytest.mark.django_db
def test_reorder_unknown_price_is_not_zero_and_zero_stock_is_supply_inquiry(public_catalog):
    customer = make_customer("Алиса")
    part = public_catalog.part("Гильза", article="G-1", price=None)
    lot = public_catalog.stock(part, "1")
    sale = make_sale(customer, part, lot=lot, quantity="1", unit_price="900")
    _identity(customer, 4001, admin=public_catalog.user)

    preview = build_reorder_preview(provider=Provider.MAX, provider_user_id=4001, sale_id=sale.pk)
    assert preview.lines[0].current_unit_price is None
    assert preview.lines[0].current_total is None
    assert preview.total is None

    # The same public part is now out of stock. The current price remains
    # unknown and the preview asks for supply instead of inventing 0 ₽.
    lot.quantity = Decimal("0")
    lot.save(update_fields=["quantity"])
    preview = build_reorder_preview(provider=Provider.MAX, provider_user_id=4001, sale_id=sale.pk)
    assert preview.lines[0].supply_inquiry is True
    assert preview.lines[0].requested_quantity == Decimal("0")
    assert preview.lines[0].current_unit_price is None


@pytest.mark.django_db
def test_confirmation_creates_only_new_request_and_binds_messenger(public_catalog):
    customer = make_customer("Алиса",)
    customer.phone = "+7 900 000-00-01"
    customer.save(update_fields=["phone"])
    part = public_catalog.part("Фильтр", article="A-2", price="1500")
    lot = public_catalog.stock(part, "5")
    sale = make_sale(customer, part, lot=lot, quantity="2", unit_price="1200")
    _identity(customer, 5001, provider=Provider.TELEGRAM, admin=public_catalog.user)

    request, created = create_request_from_reorder_preview(
        provider=Provider.TELEGRAM,
        provider_user_id=5001,
        sale_id=sale.pk,
        submission_key="messenger-reorder-tg-5001-confirmation-1",
    )
    assert created is True
    assert request.lines.get(part_type=part).price_seen == Decimal("1500")
    assert TelegramConversation.objects.filter(
        request=request, customer_user_id=5001, status=TelegramConversation.Status.LINKED
    ).exists()
    assert not MaxConversation.objects.filter(request=request).exists()
    assert not request.__class__.objects.filter(source="sale").exists()

    same, created_again = create_request_from_reorder_preview(
        provider=Provider.TELEGRAM,
        provider_user_id=5001,
        sale_id=sale.pk,
        submission_key="messenger-reorder-tg-5001-confirmation-1",
    )
    assert same.pk == request.pk
    assert created_again is False


@pytest.mark.django_db
def test_telegram_and_max_use_the_same_purchase_menu_contract(public_catalog):
    customer = make_customer("Алиса")
    part = public_catalog.part("Фильтр", article="A-3", price="1500")
    lot = public_catalog.stock(part, "5")
    sale = make_sale(customer, part, lot=lot, quantity="1", unit_price="1200")
    account = _identity(customer, 6001, provider=Provider.TELEGRAM, admin=public_catalog.user)
    CustomerIdentity.objects.create(
        account=account,
        provider=Provider.MAX,
        provider_user_id=6002,
        verified_at=timezone.now(),
    )

    telegram = telegram_service.purchase_selector_result(6001)
    max_text, max_buttons = max_service.purchase_selector_view(6002)
    assert f"Покупка №{sale.number}" in telegram.reply
    assert f"Покупка №{sale.number}" in max_text
    assert telegram.keyboard["inline_keyboard"][0][0]["callback_data"] == f"p:{sale.pk}"
    assert max_buttons[0][0]["payload"] == f"p:{sale.pk}"
