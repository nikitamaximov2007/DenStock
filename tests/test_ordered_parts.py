"""«Запчасти на заказ»: заказ оригинальной детали под предоплату клиента.

Раздел отвечает на один разговор: клиент звонит и просит привезти оригинальную
деталь, которой на складе нет, и переводит предоплату. Здесь закреплено, что
из этого следует и, главное, чего НЕ следует.

Не следует ничего складского. Заказ не создаёт ни лота, ни движения, ни
продажи, ни ремонта: детали ещё нет, и притворяться, что она есть, нельзя.

Предоплата остаётся справочной суммой заказа. Ни себестоимостью, ни ценой
продажи, ни таможенной стоимостью она не становится: на границе объявляют
стоимость товара, а не то, сколько клиент успел перевести.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.catalog_import.models import AftermarketCatalogPart
from apps.customers.models import Customer
from apps.inventory.models import StockLot, StockMovement
from apps.ordered_parts.models import OrderedPart
from apps.ordered_parts.services import (
    OrderedPartError,
    create_ordered_part,
    parse_prepayment,
    resolve_ordered_article,
)
from apps.repairs.models import RepairIssueLine
from apps.sales.models import Sale, SaleLine

PASSWORD = "parol-12345"


# --- Обстановка ------------------------------------------------------------


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(
                username=username, password=PASSWORD
            )
        user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def env(db, make_user):
    admin = make_user("boss", is_superuser=True)
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    return {"admin": admin, "cat": category, "unit": Unit.objects.get(name="Штука")}


def _part(env, *, number, name="ДЕТАЛЬ", brand="BRP"):
    manufacturer = Manufacturer.objects.get_or_create(name=brand)[0] if brand else None
    part = PartType.objects.create(
        name=name, category=env["cat"], unit=env["unit"], manufacturer=manufacturer,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("1000"),
    )
    PartNumber.objects.create(
        part=part, value=number, kind=PartNumber.Kind.OEM, is_primary=True
    )
    return part


def _analog_part(env, *, number, name="АНАЛОГ"):
    part = _part(env, number=number, name=name, brand="WOODYS")
    AftermarketCatalogPart.objects.create(
        part=part, source="dealer_2023", manufacturer=part.manufacturer,
        manufacturer_number=number, source_description=name,
        dealer_cost_usd=Decimal("10.00"),
    )
    return part


def _customer(name="Иванов Иван", phone="+7 912 123-45-67"):
    return Customer.objects.create(name=name, phone=phone)


def _order(env, part_number, customer, prepayment="1000"):
    candidate, _ = resolve_ordered_article(part_number)
    return create_ordered_part(
        candidate=candidate, customer=customer, prepayment=prepayment, by=env["admin"]
    )


def _login(client, make_user, *, role=None, name="boss"):
    if name != "boss":
        make_user(name, role=role)
    client.login(username=name, password=PASSWORD)


# --- 1-6. Создание заказа --------------------------------------------------


def test_create_ordered_part_by_exact_catalog_article(env):
    _part(env, number="219800345", name="РЕМЕНЬ ПРИВОДНОЙ")
    customer = _customer()

    order = _order(env, "219800345", customer)

    assert order.article == "219800345"
    assert order.part_name == "РЕМЕНЬ ПРИВОДНОЙ"
    assert order.manufacturer_name == "BRP"
    assert OrderedPart.objects.count() == 1


def test_customer_is_kept_by_stable_primary_key(env):
    _part(env, number="219800345")
    customer = _customer()

    order = _order(env, "219800345", customer)

    order.refresh_from_db()
    assert order.customer_id == customer.pk
    # Переименование карточки не рвёт связь: она держится за PK, а не за текст.
    customer.name = "Иванов И. И."
    customer.save(update_fields=["name"])
    order.refresh_from_db()
    assert order.customer.name == "Иванов И. И."


def test_prepayment_is_stored_as_decimal(env):
    _part(env, number="219800345")

    order = _order(env, "219800345", _customer(), prepayment="12345.67")

    order.refresh_from_db()
    assert order.prepayment_rub == Decimal("12345.67")
    assert isinstance(order.prepayment_rub, Decimal)


def test_zero_prepayment_is_allowed(env):
    """Клиент мог договориться и ещё не перевести: это не ошибка ввода."""
    _part(env, number="219800345")

    order = _order(env, "219800345", _customer(), prepayment="0")

    assert order.prepayment_rub == Decimal("0")


def test_negative_prepayment_is_rejected(env):
    _part(env, number="219800345")

    with pytest.raises(OrderedPartError, match="отрицательной"):
        _order(env, "219800345", _customer(), prepayment="-1")

    assert OrderedPart.objects.count() == 0


def test_unknown_article_is_rejected_without_inventing_a_part(env):
    before = PartType.objects.count()

    with pytest.raises(OrderedPartError, match="не найдена"):
        resolve_ordered_article("НЕТ-ТАКОГО-АРТИКУЛА")

    assert PartType.objects.count() == before  # фиктивная карточка не создана
    assert OrderedPart.objects.count() == 0


# --- 7-8. Разрешение артикула ----------------------------------------------


def test_alias_resolution_follows_the_existing_approved_rule(env):
    """Псевдоним по умолчанию не подставляется: правило поиска общее со складом."""
    part = _part(env, number="219800345")
    PartNumber.objects.create(
        part=part, value="ALIAS-777", kind=PartNumber.Kind.ANALOG, is_primary=False
    )

    # Точный номер находит деталь.
    candidate, _ = resolve_ordered_article("219800345")
    assert candidate.part.pk == part.pk

    # Вспомогательный номер каноническим поиском строго не разрешается.
    with pytest.raises(OrderedPartError):
        resolve_ordered_article("ALIAS-777")


def test_ambiguous_article_is_not_guessed(env):
    """Две карточки под одним номером: выбор остаётся за оператором."""
    first = _part(env, number="WH-100", name="ПЕРВАЯ")
    second = _part(env, number="WH-100", name="ВТОРАЯ")
    assert first.pk != second.pk

    with pytest.raises(OrderedPartError, match="Выберите"):
        resolve_ordered_article("WH-100")

    assert OrderedPart.objects.count() == 0


def test_an_analog_part_cannot_be_ordered(env):
    """Раздел про оригиналы: аналог не превращается в оригинал молча."""
    _analog_part(env, number="SM-09374")

    with pytest.raises(OrderedPartError, match="каталогом аналогов"):
        resolve_ordered_article("SM-09374")

    assert OrderedPart.objects.count() == 0


# --- 9-12. Склад не затронут -----------------------------------------------


def test_ordering_changes_no_warehouse_stock(env):
    _part(env, number="219800345")
    before = (StockLot.objects.count(), StockMovement.objects.count())

    _order(env, "219800345", _customer())

    assert (StockLot.objects.count(), StockMovement.objects.count()) == before


def test_ordering_creates_no_stock_movement(env):
    _part(env, number="219800345")

    _order(env, "219800345", _customer())

    assert StockMovement.objects.count() == 0


def test_ordering_creates_no_sale(env):
    _part(env, number="219800345")

    _order(env, "219800345", _customer())

    assert Sale.objects.count() == 0
    assert SaleLine.objects.count() == 0


def test_ordering_creates_no_repair(env):
    _part(env, number="219800345")

    _order(env, "219800345", _customer())

    assert RepairIssueLine.objects.count() == 0


# --- 13-15. Повторы и тёзки ------------------------------------------------


def test_multiple_orders_for_the_same_article_are_allowed(env):
    """Две одинаковые детали это две записи: поля количества в V1 нет."""
    _part(env, number="219800345")
    customer = _customer()

    _order(env, "219800345", customer)
    _order(env, "219800345", customer)

    assert OrderedPart.objects.filter(article="219800345").count() == 2


def test_multiple_orders_for_the_same_customer_are_allowed(env):
    _part(env, number="219800345")
    _part(env, number="219800346", name="ВТОРАЯ")
    customer = _customer()

    _order(env, "219800345", customer)
    _order(env, "219800346", customer)

    assert OrderedPart.objects.filter(customer=customer).count() == 2


def test_customer_namesakes_do_not_break_stable_selection(env):
    """Тёзки с одним телефоном это норма справочника, а не ошибка."""
    _part(env, number="219800345")
    first = _customer("Иванов Иван", "+7 912 000-00-00")
    second = _customer("Иванов Иван", "+7 912 000-00-00")
    assert first.pk != second.pk

    order = _order(env, "219800345", second)

    assert order.customer_id == second.pk
    assert OrderedPart.objects.filter(customer=first).count() == 0


def test_prepayment_parser_rejects_nonsense(env):
    assert parse_prepayment("") == Decimal("0")
    assert parse_prepayment(None) == Decimal("0")
    assert parse_prepayment("1 234,50") == Decimal("1234.50")
    for bad in ("abc", "1e", "--5"):
        with pytest.raises(OrderedPartError):
            parse_prepayment(bad)


# --- 16-24. Интерфейс и права ----------------------------------------------


def test_sidebar_shows_the_section_to_an_authorized_operator(client, env, make_user):
    _login(client, make_user)

    html = client.get(reverse("ordered_part_list")).content.decode()

    assert "Запчасти на заказ" in html
    assert reverse("ordered_part_list") in html


def test_list_page_loads(client, env, make_user):
    _part(env, number="219800345")
    _order(env, "219800345", _customer(), prepayment="500")
    _login(client, make_user)

    response = client.get(reverse("ordered_part_list"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "219800345" in html
    assert "Иванов Иван" in html


def test_create_page_loads(client, env, make_user):
    _login(client, make_user)

    response = client.get(reverse("ordered_part_create"))

    assert response.status_code == 200
    assert "Артикул запчасти" in response.content.decode()


def test_customer_search_filters_the_selectable_list(client, env, make_user):
    _customer("Иванов Иван", "+7 912 111-11-11")
    _customer("Петров Пётр", "+7 912 222-22-22")
    _login(client, make_user)

    html = client.get(
        reverse("ordered_part_create"), {"customer_q": "Петров"}
    ).content.decode()

    assert "Петров Пётр" in html
    assert "Иванов Иван" not in html


def test_existing_customer_can_be_selected_and_order_created(client, env, make_user):
    _part(env, number="219800345")
    customer = _customer()
    _login(client, make_user)

    response = client.post(
        reverse("ordered_part_create"),
        {
            "action": "create", "article": "219800345",
            "customer_id": str(customer.pk), "prepayment": "2500.50",
        },
    )

    assert response.status_code == 302
    order = OrderedPart.objects.get()
    assert order.customer_id == customer.pk
    assert order.prepayment_rub == Decimal("2500.50")


def test_a_newly_created_customer_becomes_selectable(client, env, make_user):
    """Клиент заводится обычным справочником и сразу доступен для выбора."""
    _part(env, number="219800345")
    _login(client, make_user)
    target = reverse("ordered_part_create")

    created = client.post(
        f"{reverse('customer_create')}?next={target}",
        {"name": "Сидоров Сидор", "phone": "+7 912 333-33-33", "comment": ""},
    )

    assert created.status_code == 302
    customer = Customer.objects.get(name="Сидоров Сидор")
    # Возврат в форму заказа приносит выбранную карточку с собой.
    assert f"customer_id={customer.pk}" in created.url
    html = client.get(target).content.decode()
    assert "Сидоров Сидор" in html


def test_article_lookup_shows_the_catalog_part(client, env, make_user):
    _part(env, number="219800345", name="РЕМЕНЬ ПРИВОДНОЙ")
    _login(client, make_user)

    html = client.get(
        reverse("ordered_part_create"), {"article": "219800345"}
    ).content.decode()

    assert "РЕМЕНЬ ПРИВОДНОЙ" in html
    assert "Выбранная деталь" in html


def test_invalid_article_gives_a_clear_message(client, env, make_user):
    _login(client, make_user)

    html = client.get(
        reverse("ordered_part_create"), {"article": "НЕТ-ТАКОГО"}
    ).content.decode()

    assert "не найдена" in html
    assert "Выбранная деталь" not in html


def test_anonymous_access_is_refused(client, env):
    for url in (
        reverse("ordered_part_list"),
        reverse("ordered_part_create"),
    ):
        response = client.get(url)
        assert response.status_code == 302
        assert "login" in response.url


def test_a_role_without_sales_rights_is_refused(client, env, make_user):
    _login(client, make_user, role=roles.VIEWER, name="viewer")

    assert client.get(reverse("ordered_part_list")).status_code == 403
    assert client.get(reverse("ordered_part_create")).status_code == 403


def test_the_operator_can_fix_the_customer_and_the_prepayment(client, env, make_user):
    _part(env, number="219800345")
    order = _order(env, "219800345", _customer(), prepayment="100")
    other = _customer("Петров Пётр", "+7 912 222-22-22")
    _login(client, make_user)

    response = client.post(
        reverse("ordered_part_edit", args=[order.pk]),
        {"customer_id": str(other.pk), "prepayment": "999.99"},
    )

    assert response.status_code == 302
    order.refresh_from_db()
    assert order.customer_id == other.pk
    assert order.prepayment_rub == Decimal("999.99")
    # Деталь заказа правкой не подменяется.
    assert order.article == "219800345"


def test_the_analog_gate_defers_to_the_customs_classifier_when_it_exists(env, monkeypatch):
    """Второго контракта аналогов быть не должно.

    Таможенную выгрузку делят на обычную и аналоговую в соседней ветке, и там
    появляется свой канонический ответ. Если раздел заказов оставит собственное
    правило, деталь можно будет заказать как оригинал, а её же продажа уйдёт в
    аналоговую выгрузку. Поэтому раздел спрашивает канонический классификатор,
    как только тот появляется в сборке.
    """
    import apps.ordered_parts.services as services

    part = _part(env, number="SM-01357", name="СТАТОР", brand="SPI")
    assert not services.aftermarket_part_ids([part.pk])  # каталог о ней не знает

    # Классификатор назвал деталь аналогом: заказать её нельзя.
    monkeypatch.setattr(services, "_customs_analog_verdict", lambda _part: True)
    assert services.is_analog_part(part) is True
    with pytest.raises(OrderedPartError, match="каталогом аналогов"):
        resolve_ordered_article("SM-01357")

    # Он же назвал её оригиналом: заказ проходит.
    monkeypatch.setattr(services, "_customs_analog_verdict", lambda _part: False)
    assert services.is_analog_part(part) is False
    candidate, _ = resolve_ordered_article("SM-01357")
    assert candidate.part.pk == part.pk


def test_a_missing_customs_classifier_leaves_the_base_rule_alone(env, monkeypatch):
    """Классификатора в сборке ещё нет: раздел работает по каталогу аналогов.

    Так выглядит текущая ветка до вливания таможенного разделения. Проверка не
    зависит от того, есть классификатор в сборке или нет: его ответ подменяется
    явно, иначе тест сломался бы ровно в момент интеграции.
    """
    import apps.ordered_parts.services as services

    original = _part(env, number="219800345")
    analog = _analog_part(env, number="SM-09374")

    monkeypatch.setattr(services, "_customs_analog_verdict", lambda _part: None)

    assert services.is_analog_part(original) is False
    assert services.is_analog_part(analog) is True
