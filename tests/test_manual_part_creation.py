"""Оператор заводит деталь, которой нет в каталоге.

Возможность формально была: кнопка «Добавить деталь» вела на полную карточку.
Работать по ней было нельзя по двум причинам сразу.

Первая: артикула в той форме нет вовсе. Номер детали живёт отдельной моделью и
добавляется уже после сохранения карточки, со второго экрана. Оператор, у
которого на руках коробка с номером, этот номер ввести не мог.

Вторая: категория у карточки обязательна, а справочник категорий ничем не
заполняется. На чистой системе выбрать нечего, и форма не отправляется вовсе.

Здесь закреплён рабочий сценарий: три поля, деталь появляется в каталоге и в
поиске, остаток на складе при этом не появляется.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.forms import ManualPartForm, PartTypeForm
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.catalog.services import (
    MANUAL_CATEGORY_NAME,
    ManualPartError,
    create_manual_part,
)
from apps.core.part_lookup import resolve_part_lookup
from apps.customers.models import Customer
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.models import StockReturnLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
CREATE_URL = "part_create"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def boss(client, make_user):
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)
    return client


def _post(client, **over):
    data = {"name": "Ремень вариатора", "article": "417300383", "price": "4500"}
    data.update(over)
    return client.post(reverse(CREATE_URL), data)


def _stock_snapshot():
    return (
        StockMovement.objects.count(),
        StockLot.objects.count(),
        PartItem.objects.count(),
        StockBalance.objects.count(),
    )


def _receive(part, admin, *, quantity="4", unit_cost="1000", code="S07-D01-C03"):
    """Настоящая приёмка: только она создаёт остаток и закупочную стоимость."""
    supplier = Supplier.objects.create(name=f"Поставщик {code}")
    location = StorageLocation.objects.create(
        name=f"Ячейка {code}", code=code, storage_allowed=True, is_active=True
    )
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(quantity))
    receive_stock_lot(lot, by=admin)
    return lot


# --- Почему прежняя форма не работала -------------------------------------------------


def test_the_full_card_still_has_no_field_for_an_article(db):
    """Причина первая, закреплённая как факт: вводить номер было негде."""
    assert "article" not in PartTypeForm().fields
    assert "numbers" not in PartTypeForm().fields


def test_nothing_seeds_the_category_reference(db):
    """Причина вторая: категория обязательна, а взять её неоткуда."""
    assert not Category.objects.exists(), "категории появились: проверка устарела"
    assert PartType._meta.get_field("category").null is False


def test_a_part_is_created_even_when_no_category_exists(boss, db):
    """Ровно тот случай, на котором прежняя форма останавливалась."""
    assert not Category.objects.exists()
    response = _post(boss)
    assert response.status_code == 302
    assert PartType.objects.filter(name="Ремень вариатора").exists()


# --- Обычный сценарий -----------------------------------------------------------------


def test_the_form_asks_for_exactly_three_things(db):
    visible = [field.name for field in ManualPartForm().visible_fields()]
    assert visible == ["name", "article", "price"]


def test_the_operator_fills_three_fields_and_gets_a_part(boss, db):
    response = _post(boss)
    part = PartType.objects.get(name="Ремень вариатора")
    assert response.status_code == 302
    assert response["Location"] == reverse("part_detail", args=[part.pk])


def test_only_the_name_is_required(boss, db):
    response = _post(boss, article="", price="")
    assert response.status_code == 302
    part = PartType.objects.get(name="Ремень вариатора")
    assert not part.numbers.exists()
    # Пустая цена - это отсутствие цены, а не ноль: ноль означал бы «отдаём даром».
    assert part.recommended_price is None


def test_the_price_lands_in_the_existing_catalog_price_field(boss, db):
    _post(boss, price="4500,50")
    part = PartType.objects.get(name="Ремень вариатора")
    assert part.recommended_price == Decimal("4500.50")
    # Новой финансовой величины не заводится: это то же поле, что заполняет
    # продвижение позиции из каталога поставщика.
    assert part.min_price is None


def test_the_article_becomes_a_searchable_exact_number(boss, db):
    _post(boss)
    number = PartNumber.objects.get(part__name="Ремень вариатора")
    assert number.kind == PartNumber.Kind.ARTICLE
    assert number.is_primary is True
    assert number.normalized_value == "417300383"


def test_the_new_part_is_found_by_its_article(boss, db):
    _post(boss)
    found = resolve_part_lookup("417300383", allow_partial=True, allow_name=True)
    assert [candidate.part.name for candidate in found.candidates] == ["Ремень вариатора"]


def test_the_new_part_is_found_by_its_name(boss, db):
    _post(boss)
    found = resolve_part_lookup("вариатора", allow_partial=True, allow_name=True)
    assert "Ремень вариатора" in [candidate.part.name for candidate in found.candidates]


def test_the_new_part_shows_up_in_the_catalog_list(boss, db):
    _post(boss)
    body = boss.get(reverse("part_list")).content.decode()
    assert "Ремень вариатора" in body
    assert "417300383" in body


# --- Карточка - это ещё не остаток ----------------------------------------------------


def test_creating_a_card_creates_no_stock(boss, db):
    before = _stock_snapshot()
    _post(boss)
    assert _stock_snapshot() == before


def test_the_operator_is_told_that_there_is_no_stock_yet(boss, db):
    response = _post(boss, price="")
    texts = [str(message) for message in response.wsgi_request._messages]
    assert any("Ремень вариатора" in text for text in texts), "непонятно, что именно создано"
    assert any("остат" in text.lower() for text in texts), "не сказано про отсутствие остатка"


def test_the_page_does_not_leak_template_comments(boss, db):
    """Многострочный комментарий вида {# … #} Django не распознаёт.

    Найдено на живой странице: пояснение о том, как передаётся подтверждение
    совпадения, оказалось на экране между полями и кнопкой. По исходнику это
    не видно - комментарий выглядит правильным.
    """
    body = boss.get(reverse(CREATE_URL)).content.decode()
    assert "{#" not in body
    assert "{%" not in body


def test_the_duplicate_page_does_not_leak_template_comments(boss, db):
    _post(boss)
    body = _post(boss, name="Ремень другой").content.decode()
    assert "{#" not in body
    assert "{%" not in body


# --- Проверка введённого ---------------------------------------------------------------


def test_stray_spaces_do_not_create_a_second_looking_part(boss, db):
    _post(boss, name="  Ремень   вариатора  ", article="  417300383  ")
    part = PartType.objects.get()
    assert part.name == "Ремень вариатора"
    assert part.numbers.get().value == "417300383"


def test_an_empty_name_is_refused_without_creating_anything(boss, db):
    response = _post(boss, name="   ")
    assert response.status_code == 200
    assert not PartType.objects.exists()


@pytest.mark.parametrize("bad_price", ["-1", "-0.01", "не число", "1e400"])
def test_a_bad_price_is_a_message_and_not_a_crash(boss, db, bad_price):
    response = _post(boss, price=bad_price)
    assert response.status_code == 200, f"цена «{bad_price}» уронила страницу"
    assert not PartType.objects.exists()


def test_a_comma_is_accepted_as_a_decimal_separator(boss, db):
    """На складской клавиатуре запятая ближе, и так набирают чаще."""
    _post(boss, price="4500,50")
    assert PartType.objects.get().recommended_price == Decimal("4500.50")


def test_the_service_refuses_a_negative_price_on_its_own(db):
    with pytest.raises(ManualPartError):
        create_manual_part(name="Ремень", price=Decimal("-1"))


def test_the_service_refuses_an_unstorable_price(db):
    """Через форму такое число не пройдёт, но служба вызывается и из кода."""
    with pytest.raises(ManualPartError):
        create_manual_part(name="Ремень", price=Decimal("1e30"))


def test_the_service_says_what_is_wrong_when_there_are_no_units(db):
    """Ошибка окружения тоже должна быть объяснимой, а не страницей ошибки."""
    Unit.objects.all().delete()
    with pytest.raises(ManualPartError) as failure:
        create_manual_part(name="Ремень")
    assert "единиц" in str(failure.value)


# --- Совпадение артикула ---------------------------------------------------------------


def test_an_existing_article_stops_the_operator_and_shows_what_is_there(boss, db):
    _post(boss)
    response = _post(boss, name="Ремень другой")

    assert response.status_code == 200
    assert PartType.objects.count() == 1, "вторая деталь завелась молча"
    body = response.content.decode()
    assert "уже есть" in body
    assert "Ремень вариатора" in body, "не показано, какая именно деталь нашлась"


def test_the_operator_can_still_insist(boss, db):
    """Одинаковые номера у разных производителей - обычное дело."""
    _post(boss)
    response = boss.post(
        reverse(CREATE_URL),
        {"name": "Ремень другой", "article": "417300383",
         "price": "4500", "confirm_duplicate": "1"},
    )
    assert response.status_code == 302
    assert PartType.objects.count() == 2


def test_the_return_address_survives_the_duplicate_warning(boss, db):
    """Оператор, пришедший из черновика поступления, должен вернуться туда же.

    Предупреждение о совпадении - это ещё один заход на ту же страницу, и
    адрес возврата не должен по дороге потеряться.
    """
    back = reverse("part_list")
    url = f"{reverse(CREATE_URL)}?next={back}"
    boss.post(url, {"name": "Ремень вариатора", "article": "417300383", "price": ""})

    warned = boss.post(url, {"name": "Ремень другой", "article": "417300383", "price": ""})
    assert warned.status_code == 200
    # Своего адреса отправки у формы нет, поэтому подтверждение уходит по тому
    # же адресу вместе с «next».
    assert '<form method="post" class="form">' in warned.content.decode()

    done = boss.post(url, {"name": "Ремень другой", "article": "417300383",
                           "price": "", "confirm_duplicate": "1"})
    part = PartType.objects.get(name="Ремень другой")
    assert done["Location"] == f"{back}?new_part={part.pk}"


def test_a_matching_number_is_shown_as_a_notice_and_not_as_a_failure(boss, db):
    """Совпадение - повод посмотреть, а не сообщение о том, что всё сломалось.

    Найдено на живой странице: подсказка выводилась красным блоком отказа,
    хотя оператор ничего неправильного не сделал и через одно нажатие
    продолжает работу.
    """
    _post(boss)
    body = _post(boss, name="Ремень другой").content.decode()

    assert "status--warning" in body, "подсказка о совпадении не показана предупреждением"
    assert "status--error" not in body, "обычное совпадение показано как отказ"


def test_a_real_failure_is_still_shown_as_a_failure(boss, db):
    Unit.objects.update(is_active=False)
    body = _post(boss, article="").content.decode()

    assert "status--error" in body
    assert "status--warning" not in body


def test_a_confirmed_duplicate_still_shows_a_real_failure(boss, db):
    """Причина отказа не должна теряться за списком совпадений.

    Иначе оператор, уже подтвердивший совпадение, жал бы кнопку впустую: экран
    выглядел бы так же, как до нажатия.
    """
    _post(boss)
    Unit.objects.update(is_active=False)

    response = boss.post(
        reverse(CREATE_URL),
        {"name": "Ремень другой", "article": "417300383",
         "price": "", "confirm_duplicate": "1"},
    )
    assert response.status_code == 200
    assert "единиц" in response.content.decode(), "причина отказа не показана"
    assert PartType.objects.count() == 1


def test_a_different_article_is_not_treated_as_a_duplicate(boss, db):
    _post(boss)
    response = _post(boss, name="Свеча", article="417300384")
    assert response.status_code == 302
    assert PartType.objects.count() == 2


def test_no_uniqueness_was_quietly_introduced_on_numbers(db):
    """Уникальность номера сломала бы существующие данные, и её здесь нет.

    Совпадение показывается человеку, а решает он.
    """
    assert PartNumber._meta.get_field("value").unique is False
    limits = list(PartNumber._meta.constraints) + list(PartNumber._meta.unique_together)
    assert not limits, f"на номерах появилось ограничение: {limits}"


def test_a_supplier_price_refresh_does_not_touch_a_manual_price(boss, db):
    """Цена, введённая человеком, не должна переписываться пересчётом.

    Пересчёт идёт по связям с каталогами поставщиков, а у заведённой вручную
    детали таких связей нет. Закрепляется, потому что обратное было бы
    незаметно: цена просто изменилась бы однажды ночью.
    """
    from apps.catalog.services import refresh_linked_part_prices

    _post(boss, price="4500")
    refresh_linked_part_prices(
        usd_rate=Decimal("100"), brp_markup=Decimal("50"), polaris_markup=Decimal("50")
    )
    assert PartType.objects.get().recommended_price == Decimal("4500.00")


# --- Права и остальная карточка ---------------------------------------------------------


def test_a_storekeeper_still_cannot_create_parts(client, make_user, db):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    response = _post(client)
    assert response.status_code == 403
    assert not PartType.objects.exists()


def test_the_new_part_goes_all_the_way_through_receipt_and_sale(boss, make_user, db):
    """Смысл сценария не в карточке, а в том, что деталью потом работают.

    Проходится весь путь: заведение, приёмка, продажа. Если бы карточке не
    хватало чего-то из подставленного - единицы, категории, режима учёта, - он
    оборвался бы на одном из шагов.
    """
    _post(boss, price="4500")
    part = PartType.objects.get()
    admin = make_user("sklad-boss", is_superuser=True)

    supplier = Supplier.objects.create(name="ООО Поставка")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S07-D01-C02", storage_allowed=True, is_active=True
    )
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal("4"), unit_cost_currency=Decimal("1000"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("4"))
    receive_stock_lot(lot, by=admin)

    sale = create_sale(customer=None, customer_name="Иванов", by=admin)
    add_stock_lot_to_sale(sale, lot, Decimal("2"), unit_price=Decimal("4500"), by=admin)
    sale = complete_sale(sale, by=admin)

    assert sale.lines.get().total_price == Decimal("9000")
    lot.refresh_from_db()
    assert lot.quantity == Decimal("2"), "продажа не списала остаток"
    balance = StockBalance.objects.get(part_type=part, location=location)
    assert balance.quantity_available == Decimal("2")


def test_the_new_part_goes_all_the_way_through_receipt_and_repair(boss, make_user, db):
    """Второй настоящий путь: заведение, приёмка, выдача в ремонт.

    У ремонта своя заморозка себестоимости, и она должна взять закупочную цену
    приёмки, а не цену продажи из карточки. Это две разные величины, и путать
    их нельзя.
    """
    _post(boss, price="4500")
    part = PartType.objects.get()
    admin = make_user("remont-boss", is_superuser=True)
    lot = _receive(part, admin, quantity="4", unit_cost="1000")

    customer = Customer.objects.create(name="Иванов")
    order = create_repair_order(customer=customer, customer_name="", by=admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("2"), by=admin)
    order = complete_repair_order(order, by=admin)

    issued = order.lines.get()
    assert issued.unit_cost_rub == Decimal("1000.00"), "взята не закупочная цена"
    assert issued.total_cost_rub == Decimal("2000.00")
    assert issued.unit_cost_rub != part.recommended_price, (
        "цена продажи из карточки просочилась в себестоимость ремонта"
    )
    lot.refresh_from_db()
    assert lot.quantity == Decimal("2"), "выдача не списала остаток"


def test_a_returned_manual_part_comes_back_to_the_shelf(boss, make_user, db):
    """Обратный путь у проданной детали - возврат, и он тоже должен работать.

    Отмены проведённой продажи в продукте нет: проведённый документ неизменяем,
    а деталь возвращается отдельным возвратом. Ручная деталь должна проходить
    его наравне с любой другой.
    """
    _post(boss, price="4500")
    part = PartType.objects.get()
    admin = make_user("vozvrat-boss", is_superuser=True)
    lot = _receive(part, admin, quantity="5", unit_cost="1000")

    sale = create_sale(customer=None, customer_name="Иванов", by=admin)
    add_stock_lot_to_sale(sale, lot, Decimal("3"), unit_price=Decimal("4500"), by=admin)
    sale = complete_sale(sale, by=admin)
    balance = StockBalance.objects.get(part_type=part, location=lot.location)
    assert balance.quantity_available == Decimal("2")

    ret = create_return(source=sale, by=admin)
    add_sale_line_return(
        ret, sale.lines.get(), Decimal("3"),
        to_location=lot.location,
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=admin,
    )
    complete_return(ret, by=admin)

    balance.refresh_from_db()
    assert balance.quantity_available == Decimal("5"), "остаток не вернулся"


def test_the_catalog_price_does_not_become_the_incoming_cost(boss, make_user, db):
    """Цена в карточке - продажная. Себестоимость появляется только на приёмке."""
    _post(boss, price="4500")
    part = PartType.objects.get()
    admin = make_user("prihod-boss", is_superuser=True)
    lot = _receive(part, admin, quantity="2", unit_cost="900")

    assert lot.landed_unit_cost_rub == Decimal("900.00")
    assert part.recommended_price == Decimal("4500.00")


def test_changing_the_catalog_price_later_leaves_a_past_repair_alone(boss, make_user, db):
    """Историческая величина не переписывается задним числом."""
    _post(boss, price="4500")
    part = PartType.objects.get()
    admin = make_user("istoriya-boss", is_superuser=True)
    lot = _receive(part, admin, quantity="4", unit_cost="1000")

    customer = Customer.objects.create(name="Иванов")
    order = create_repair_order(customer=customer, customer_name="", by=admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("1"), by=admin)
    order = complete_repair_order(order, by=admin)
    before = order.lines.get().total_cost_rub

    part.recommended_price = Decimal("99999")
    part.save(update_fields=["recommended_price"])
    order.lines.get().refresh_from_db()
    assert order.lines.get().total_cost_rub == before


# --- Совпадение артикула: как его на самом деле пишут -------------------------------------


@pytest.mark.parametrize(
    "written_again",
    ["417300383", "417-300-383", "417 300 383", " 417300383 ", "417.300.383"],
    ids=["как есть", "через дефис", "через пробел", "с пробелами по краям", "через точку"],
)
def test_the_same_number_written_differently_is_still_recognised(boss, db, written_again):
    """Оператор редко повторяет запись символ в символ."""
    _post(boss)
    response = _post(boss, name="Ремень другой", article=written_again)

    assert response.status_code == 200, f"«{written_again}» не опознан как тот же номер"
    assert PartType.objects.count() == 1


def test_a_lowercase_letter_number_matches_its_uppercase_twin(boss, db):
    _post(boss, article="ABC-123")
    response = _post(boss, name="Другая", article="abc 123")
    assert response.status_code == 200
    assert PartType.objects.count() == 1


def test_a_cyrillic_number_works_the_same_way(boss, db):
    _post(boss, article="АБВ-123")
    response = _post(boss, name="Другая", article="абв123")
    assert response.status_code == 200
    assert PartType.objects.count() == 1


def test_all_existing_matches_are_shown_not_just_the_first(boss, db):
    _post(boss, name="Ремень первый")
    boss.post(
        reverse(CREATE_URL),
        {"name": "Ремень второй", "article": "417300383", "confirm_duplicate": "1"},
    )
    assert PartType.objects.count() == 2

    body = _post(boss, name="Ремень третий").content.decode()
    assert "Ремень первый" in body
    assert "Ремень второй" in body


def test_a_number_that_came_from_the_supplier_catalog_also_counts(boss, db):
    """Совпадение с уже заведённой деталью ищется по номеру, а не по способу.

    Деталь, пришедшая из каталога поставщика, хранит номер тем же видом
    точного номера, поэтому она находится наравне с заведёнными вручную.
    """
    category = Category.objects.create(name="Каталог")
    imported = PartType.objects.create(
        name="Из каталога поставщика", category=category,
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=imported, value="417300383", kind=PartNumber.Kind.OEM, is_primary=True
    )

    response = _post(boss)
    assert response.status_code == 200
    assert "Из каталога поставщика" in response.content.decode()
    assert PartType.objects.count() == 1, "завелась вторая карточка того же номера"


def test_an_archived_part_with_the_same_number_is_still_worth_showing(boss, db):
    """Отключённая карточка - повод открыть её, а не заводить дубликат."""
    category = Category.objects.create(name="Каталог")
    archived = PartType.objects.create(
        name="Снятая с учёта", category=category,
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, is_active=False,
    )
    PartNumber.objects.create(part=archived, value="417300383", kind=PartNumber.Kind.ARTICLE)

    response = _post(boss)
    assert response.status_code == 200
    assert "Снятая с учёта" in response.content.decode()


def test_an_analog_number_is_not_treated_as_the_same_part(boss, db):
    """Аналог - это «подойдёт вместо», а не «тот же номер»."""
    category = Category.objects.create(name="Каталог")
    other = PartType.objects.create(
        name="Совсем другая", category=category,
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=other, value="417300383", kind=PartNumber.Kind.ANALOG)

    response = _post(boss)
    assert response.status_code == 302, "аналог принят за совпадение номера"
    assert PartType.objects.count() == 2


def test_two_parts_without_an_article_never_collide(boss, db):
    _post(boss, name="Первая", article="")
    response = _post(boss, name="Вторая", article="")
    assert response.status_code == 302
    assert PartType.objects.count() == 2


def test_the_same_name_is_not_by_itself_a_duplicate(boss, db):
    """Названия повторяются постоянно, и запрещать это нельзя."""
    _post(boss, name="Ремень", article="111")
    response = _post(boss, name="Ремень", article="222")
    assert response.status_code == 302
    assert PartType.objects.count() == 2


# --- Цена: всё, что можно набрать в поле -----------------------------------------------


@pytest.mark.parametrize(
    "typed,stored",
    [
        ("0", Decimal("0.00")),
        ("0.00", Decimal("0.00")),
        ("4500", Decimal("4500.00")),
        ("4500.5", Decimal("4500.50")),
        ("4500,5", Decimal("4500.50")),
        ("  4500  ", Decimal("4500.00")),
    ],
    ids=["ноль", "ноль с копейками", "целое", "через точку", "через запятую", "с пробелами"],
)
def test_prices_that_should_be_accepted(boss, db, typed, stored):
    response = _post(boss, price=typed)
    assert response.status_code == 302, f"«{typed}» не принято"
    assert PartType.objects.get().recommended_price == stored


@pytest.mark.parametrize(
    "typed",
    # Арабо-индийские цифры («٤٥٠٠») сюда не входят намеренно: их разбирает
    # сама стандартная библиотека, набрать их на складской клавиатуре нельзя,
    # и запрещать их отдельным правилом не за чем.
    ["-1", "-0.01", "не число", "1e400", "4500,50,10", "4500 руб", "--5", "1/2"],
)
def test_prices_that_must_be_refused_without_a_crash(boss, db, typed):
    response = _post(boss, price=typed)
    assert response.status_code == 200, f"«{typed}» уронило страницу"
    assert not PartType.objects.exists()
    assert "{%" not in response.content.decode()


def test_too_many_decimal_places_are_refused_rather_than_silently_cut(boss, db):
    """Тихое округление денег - худший из вариантов: человек не узнает."""
    response = _post(boss, price="4500.555")
    assert response.status_code == 200
    assert not PartType.objects.exists()


def test_a_price_that_does_not_fit_the_field_is_a_message(boss, db):
    response = _post(boss, price="9" * 20)
    assert response.status_code == 200
    assert not PartType.objects.exists()


# --- Как деталь ищут потом ---------------------------------------------------------------


@pytest.mark.parametrize("query", ["417300383", "417-300-383", "Ремень", "ремень вариатора"])
def test_the_new_part_is_findable_in_the_shared_search(boss, db, query):
    _post(boss)
    response = boss.get(reverse("part_search"), {"q": query})
    assert response.status_code == 200
    assert "Ремень вариатора" in response.content.decode(), f"не нашлась по «{query}»"


def test_the_new_part_can_be_picked_when_receiving(boss, make_user, db):
    _post(boss)
    part = PartType.objects.get()
    body = boss.get(reverse("receipt_create")).content.decode()
    assert str(part.pk) in body or "Ремень вариатора" in body


def test_the_parts_list_search_finds_it_by_article(boss, db):
    _post(boss)
    body = boss.get(reverse("part_list"), {"q": "417300383"}).content.decode()
    assert "Ремень вариатора" in body


# --- Поведение формы как страницы --------------------------------------------------------


def test_opening_the_page_creates_nothing(boss, db):
    """Открытие формы - чтение. Мутация только через отправку."""
    assert boss.get(reverse(CREATE_URL)).status_code == 200
    assert not PartType.objects.exists()
    assert not Category.objects.exists()


def test_a_repeated_submission_of_the_same_form_is_the_operator_s_decision(boss, db):
    """После успеха отправка идёт с перенаправлением, поэтому обновление
    страницы результата ничего не создаёт заново.

    Если человек всё-таки отправит форму второй раз, он увидит предупреждение
    о совпадении, а не молча получит дубликат.
    """
    first = _post(boss)
    assert first.status_code == 302
    assert first["Location"].startswith("/")

    second = _post(boss)
    assert second.status_code == 200
    assert PartType.objects.count() == 1


def test_the_manual_category_is_created_once_and_reused(boss, db):
    """Двадцать одинаковых категорий в справочнике - это мусор в глазах."""
    for index in range(5):
        _post(boss, name=f"Деталь {index}", article=f"{index}")
    assert PartType.objects.count() == 5
    assert Category.objects.filter(name=MANUAL_CATEGORY_NAME).count() == 1
    assert Category.objects.count() == 1


def test_two_operators_creating_the_same_number_both_succeed(boss, db):
    """Уникальности номера нет, и одновременная работа не должна падать.

    Проверяется поведение самой службы: она не рассчитывает на то, что номер
    единственный, и не пытается это обеспечить.
    """
    first = create_manual_part(name="Ремень", article="417300383", price=Decimal("100"))
    second = create_manual_part(name="Ремень", article="417300383", price=Decimal("100"))
    assert first.pk != second.pk
    assert PartNumber.objects.filter(normalized_value="417300383").count() == 2
    assert Category.objects.filter(name=MANUAL_CATEGORY_NAME).count() == 1


def test_the_full_card_is_still_there_for_editing(boss, db):
    _post(boss)
    part = PartType.objects.get()
    body = boss.get(reverse("part_edit", args=[part.pk])).content.decode()
    for field in ("category", "manufacturer", "unit", "tracking_mode", "min_stock_level"):
        assert f'name="{field}"' in body, f"поле {field} пропало из редактирования"
