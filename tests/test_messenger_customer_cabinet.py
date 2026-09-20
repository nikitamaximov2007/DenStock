from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.customer_accounts import services as account_services
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
from apps.customer_requests.models import CustomerRequest, MaxConversation, TelegramConversation
from tests.customer_account_support import make_customer, make_sale


@pytest.fixture(autouse=True)
def messenger_cabinet_flags(settings):
    settings.CUSTOMER_MESSENGER_CABINET_ENABLED = True
    settings.CUSTOMER_MESSENGER_REPEAT_CONSENT_VERSION = "messenger-repeat-v1"


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

    lot.quantity = Decimal("0")
    lot.save(update_fields=["quantity"])
    preview = build_reorder_preview(provider=Provider.MAX, provider_user_id=4001, sale_id=sale.pk)
    assert preview.lines[0].supply_inquiry is True
    assert preview.lines[0].requested_quantity == Decimal("0")
    assert preview.lines[0].current_unit_price is None


@pytest.mark.django_db
def test_history_is_limited_to_the_latest_twelve_months(public_catalog):
    customer = make_customer("Алиса")
    part = public_catalog.part("Фильтр", article="12M", price="1500")
    lot = public_catalog.stock(part, "5")
    sale = make_sale(customer, part, lot=lot, quantity="1", unit_price="1200")
    sale.sold_at = timezone.now() - timedelta(days=366)
    sale.save(update_fields=["sold_at"])
    _identity(customer, 4501, admin=public_catalog.user)
    assert list_customer_purchases(provider=Provider.MAX, provider_user_id=4501) == ()


@pytest.mark.django_db
def test_reorder_quantity_excludes_completed_return(public_catalog):
    customer = make_customer("Алиса")
    part = public_catalog.part("Фильтр", article="RET", price="1500")
    lot = public_catalog.stock(part, "5")
    sale = make_sale(customer, part, lot=lot, quantity="3", unit_price="1200")
    _identity(customer, 4601, admin=public_catalog.user)
    from apps.returns.models import StockReturnLine
    from apps.returns.services import add_sale_line_return, complete_return, create_return

    document = create_return(source=sale, by=public_catalog.user)
    add_sale_line_return(
        document,
        sale.lines.get(),
        Decimal("2"),
        to_location=lot.location,
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=public_catalog.user,
    )
    complete_return(document, by=public_catalog.user)
    preview = build_reorder_preview(provider=Provider.MAX, provider_user_id=4601, sale_id=sale.pk)
    assert preview.lines[0].historical_quantity == Decimal("1")
    assert preview.lines[0].requested_quantity == Decimal("1")


@pytest.mark.django_db
def test_returns_are_keyed_by_sale_line_when_part_repeats(public_catalog):
    customer = make_customer("Алиса")
    part = public_catalog.part("Повторная деталь", article="DUP", price="1500")
    lot = public_catalog.stock(part, "10")
    sale = make_sale(customer, part, lot=lot, quantity="2", unit_price="1200")
    from apps.sales.models import SaleLine
    SaleLine.objects.create(
        sale=sale, part_type=part, stock_lot=lot, batch=lot.batch_line.batch,
        batch_line=lot.batch_line, quantity=Decimal("3"), unit_price=Decimal("1200"),
        total_price=Decimal("3600"), unit_cost_rub=Decimal("1"),
        total_cost_rub=Decimal("3"), profit_rub=Decimal("3597"),
    )
    _identity(customer, 4701, admin=public_catalog.user)
    from apps.returns.models import StockReturnLine
    from apps.returns.services import add_sale_line_return, complete_return, create_return
    document = create_return(source=sale, by=public_catalog.user)
    add_sale_line_return(
        document, sale.lines.order_by("pk").first(), Decimal("1"),
        to_location=lot.location, restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=public_catalog.user,
    )
    complete_return(document, by=public_catalog.user)
    preview = build_reorder_preview(provider=Provider.MAX, provider_user_id=4701, sale_id=sale.pk)
    assert [line.historical_quantity for line in preview.lines] == [Decimal("1"), Decimal("3")]
    assert [line.requested_quantity for line in preview.lines] == [Decimal("1"), Decimal("3")]


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
    assert request.source == request.Source.MESSENGER_REPEAT
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
def test_max_repeat_keeps_dialog_chat_id_distinct_from_provider_user_id(public_catalog):
    customer = make_customer("Алиса")
    customer.phone = "+7 900 000-00-01"
    customer.save(update_fields=["phone"])
    part = public_catalog.part("Фильтр", article="MAX-CHAT", price="1500")
    lot = public_catalog.stock(part, "5")
    sale = make_sale(customer, part, lot=lot, quantity="1", unit_price="1200")
    _identity(customer, 94001, provider=Provider.MAX, admin=public_catalog.user)
    text, _buttons = max_service.confirm_reorder_view(
        user_id=94001, chat_id=777777777, sale_id=str(sale.pk), callback_key="press-1"
    )
    assert "создана" in text
    request = CustomerRequest.objects.get(source=CustomerRequest.Source.MESSENGER_REPEAT)
    conversation = MaxConversation.objects.get(request=request)
    assert conversation.customer_user_id == 94001
    assert conversation.customer_chat_id == 777777777
    assert MaxConversation.objects.filter(customer_chat_id=94001).count() == 0
    assert max_service.MaxCustomerChat.objects.get(user_id=94001).chat_id == 777777777


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


@pytest.mark.django_db
def test_messenger_flag_is_separate_from_dormant_web_account(settings, public_catalog):
    settings.CUSTOMER_MESSENGER_CABINET_ENABLED = True
    settings.CUSTOMER_ACCOUNT_ENABLED = False
    settings.CUSTOMER_AUTH_MAX_ENABLED = False
    account = account_services.ensure_messenger_identity(Provider.TELEGRAM, 7001, "Алиса")
    assert account is not None
    assert CustomerIdentity.objects.filter(
        provider=Provider.TELEGRAM, provider_user_id=7001, account=account
    ).exists()
    assert not account_services.login_enabled(Provider.TELEGRAM)
    assert not account_services.login_enabled(Provider.MAX)
    from apps.customer_accounts.models import CustomerLoginAttempt, CustomerSession
    assert not CustomerSession.objects.exists()
    assert not CustomerLoginAttempt.objects.exists()


@pytest.mark.django_db
def test_messenger_cabinet_menu_is_absent_when_flag_is_off(settings):
    settings.CUSTOMER_MESSENGER_CABINET_ENABLED = False
    assert customer_ui.MY_PURCHASES_BUTTON not in str(telegram_service.customer_keyboard())
    assert customer_ui.MY_PURCHASES_BUTTON not in str(max_service.menu_button())


@pytest.mark.django_db
def test_handoff_does_not_create_ownership_pii_when_cabinet_is_off(settings, public_catalog):
    settings.CUSTOMER_MESSENGER_CABINET_ENABLED = False
    from apps.customer_accounts.messenger_hooks import max_handoff
    from apps.customer_requests.models import CustomerRequest

    request = CustomerRequest.objects.create(
        customer_name="Алиса", customer_phone="+7 900 000-00-01",
        preferred_messenger=CustomerRequest.Messenger.MAX,
        privacy_policy_version="pp", personal_data_consent_version="pd",
        consent_purpose="public_request_contact",
        consent_accepted_at=timezone.now(), submission_key_hash="a" * 64,
    )
    max_handoff(request, user_id=94001, name="Алиса")
    assert not CustomerAccount.objects.exists()
    assert not CustomerIdentity.objects.exists()


@pytest.mark.django_db
def test_staff_can_explicitly_link_and_unlink_messenger_account(client, public_catalog):
    customer = make_customer("Алиса")
    account = account_services.ensure_messenger_identity(Provider.TELEGRAM, 8001, "A")
    client.force_login(public_catalog.user)
    url = "/customer-accounts/links/"
    response = client.post(url, {"account_id": account.pk, "customer_id": customer.pk})
    assert response.status_code == 302
    assert account_services.linked_customer_id(account) == customer.pk
    response = client.post(url, {"account_id": account.pk, "action": "unlink"})
    assert response.status_code == 302
    assert account_services.linked_customer_id(account) is None
