"""Экраны аналогов глазами оператора.

Главный сценарий Дениса: на полке лежит неоригинальная деталь, на коробке тот
же артикул, что у оригинала, оригинала нет в наличии. Он ищет артикул и должен
за несколько секунд понять, что оригинал есть в каталоге, но его нет, а аналог
лежит на полке и его четыре штуки.

Здесь проверяется, что это видно на экране, а не только лежит в базе.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import PartAnalog, PartType
from apps.catalog.services import create_manual_part, link_analog
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
SAME = "420123456"


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


@pytest.fixture
def shelf(db):
    return StorageLocation.objects.create(
        name="Ячейка", code="S02-D03-C01", storage_allowed=True, is_active=True
    )


def receive(part, admin, *, quantity="4", unit_cost="2600", where=None, code="S09-D01-C01"):
    """Настоящая приёмка: только она создаёт остаток."""
    supplier = Supplier.objects.create(name=f"Поставщик {part.pk}")
    location = where or StorageLocation.objects.create(
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


@pytest.fixture
def shelf_scene(boss, make_user, shelf):
    """Ровно та полка, о которой говорит Денис."""
    admin = make_user("sklad-boss", is_superuser=True)
    original = create_manual_part(
        name="Поршень BRP", article=SAME, price=Decimal("10000"),
        barcode="4600000000011", manufacturer_name="BRP",
    )
    analog = create_manual_part(
        name="Поршень XYZ", article=SAME, price=Decimal("4500"),
        barcode="4600000000028", manufacturer_name="XYZ",
    )
    link_analog(original=original, analog=analog)
    lot = receive(analog, admin, quantity="4", unit_cost="2600", where=shelf)
    return {"client": boss, "admin": admin, "original": original,
            "analog": analog, "lot": lot, "shelf": shelf}


def _stock_snapshot():
    return (
        StockMovement.objects.count(),
        StockLot.objects.count(),
        PartItem.objects.count(),
        StockBalance.objects.count(),
    )


# --- Карточка исходной детали ------------------------------------------------------


def test_the_card_answers_whether_an_analog_is_on_the_shelf(shelf_scene):
    """Главный вопрос у прилавка. Ответ должен быть в самой строке."""
    scene = shelf_scene
    body = scene["client"].get(
        reverse("part_detail", args=[scene["original"].pk])
    ).content.decode()

    assert "Аналоги" in body
    assert "Поршень XYZ" in body
    assert "XYZ" in body
    assert "4 500" in body.replace("&nbsp;", " ")
    assert "S02-D03-C01" in body, "не видно, где именно лежит"


def test_the_card_of_the_analog_says_what_it_fits(shelf_scene):
    scene = shelf_scene
    body = scene["client"].get(
        reverse("part_detail", args=[scene["analog"].pk])
    ).content.decode()

    assert "Аналог для" in body
    assert "Поршень BRP" in body


def test_the_card_offers_to_add_an_analog(shelf_scene):
    scene = shelf_scene
    body = scene["client"].get(
        reverse("part_detail", args=[scene["original"].pk])
    ).content.decode()
    assert reverse("part_analog_add", args=[scene["original"].pk]) in body


def test_a_card_without_analogs_says_so_plainly(boss, db):
    lonely = create_manual_part(name="Одинокая", article="111")
    body = boss.get(reverse("part_detail", args=[lonely.pk])).content.decode()
    assert "Аналоги не отмечены" in body


def test_the_card_does_not_show_internal_identifiers(shelf_scene):
    """Денис не должен читать номера записей и лотов."""
    scene = shelf_scene
    body = scene["client"].get(
        reverse("part_detail", args=[scene["original"].pk])
    ).content.decode()
    for noise in ("PartAnalog", "link_id", "lot_id", "pk=", "Лот #"):
        assert noise not in body


# --- Экран добавления --------------------------------------------------------------


def test_the_add_screen_offers_both_ways_at_once(shelf_scene):
    """Человек не знает заранее, есть такая деталь в системе или нет."""
    scene = shelf_scene
    body = scene["client"].get(
        reverse("part_analog_add", args=[scene["original"].pk])
    ).content.decode()
    assert "Найти уже заведённую деталь" in body
    assert "Или завести новую" in body


def test_the_search_shows_enough_to_choose_between_equal_articles(shelf_scene):
    """Пять деталей с одним артикулом различаются заводом, ценой и наличием."""
    scene = shelf_scene
    other = create_manual_part(
        name="Поршень ABC", article=SAME, price=Decimal("3900"), manufacturer_name="ABC"
    )
    body = scene["client"].get(
        reverse("part_analog_add", args=[scene["original"].pk]), {"q": SAME}
    ).content.decode()

    assert other.name in body
    assert "ABC" in body
    assert "3 900" in body.replace("&nbsp;", " ")


def test_the_original_never_offers_to_link_itself(shelf_scene):
    scene = shelf_scene
    body = scene["client"].get(
        reverse("part_analog_add", args=[scene["original"].pk]), {"q": "Поршень BRP"}
    ).content.decode()
    assert 'value="{}"'.format(scene["original"].pk) not in body


def test_an_already_linked_part_is_marked_instead_of_offered(shelf_scene):
    scene = shelf_scene
    body = scene["client"].get(
        reverse("part_analog_add", args=[scene["original"].pk]), {"q": SAME}
    ).content.decode()
    assert "уже аналог" in body


def test_the_source_part_is_marked_before_the_click(shelf_scene):
    """Обратную связь заводить нельзя, и человек должен узнать это заранее."""
    scene = shelf_scene
    body = scene["client"].get(
        reverse("part_analog_add", args=[scene["analog"].pk]), {"q": SAME}
    ).content.decode()
    assert "это исходная деталь" in body


def test_linking_an_existing_part_takes_one_click(boss, db):
    original = create_manual_part(name="Поршень BRP", article=SAME)
    candidate = create_manual_part(name="Поршень XYZ", article=SAME)

    response = boss.post(
        reverse("part_analog_add", args=[original.pk]), {"link_part": candidate.pk}
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("part_detail", args=[original.pk])
    assert PartAnalog.objects.filter(original=original, analog=candidate).exists()


def test_creating_a_new_analog_takes_one_form(boss, db):
    original = create_manual_part(name="Поршень BRP", article=SAME)

    response = boss.post(
        reverse("part_analog_add", args=[original.pk]),
        {"name": "Поршень XYZ", "article": SAME, "price": "4500",
         "manufacturer_name": "XYZ", "barcode": "4600000000028",
         "confirm_duplicate": "1"},
    )

    assert response.status_code == 302
    made = PartType.objects.get(name="Поршень XYZ")
    assert made.manufacturer.name == "XYZ"
    assert made.barcodes.get().value == "4600000000028"
    assert PartAnalog.objects.filter(original=original, analog=made).exists()


def test_a_matching_article_is_only_a_notice_here(boss, db):
    """У аналога совпадение артикула - норма, а не повод останавливать."""
    original = create_manual_part(name="Поршень BRP", article=SAME)
    response = boss.post(
        reverse("part_analog_add", args=[original.pk]),
        {"name": "Поршень XYZ", "article": SAME, "price": "4500"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "status--warning" in body
    assert "Всё равно создать" in body
    assert PartType.objects.count() == 1


def test_the_reverse_link_is_refused_with_words_not_a_crash(shelf_scene):
    scene = shelf_scene
    response = scene["client"].post(
        reverse("part_analog_add", args=[scene["analog"].pk]),
        {"link_part": scene["original"].pk},
    )
    assert response.status_code == 302
    assert PartAnalog.objects.count() == 1


def test_removing_a_link_keeps_the_part(shelf_scene):
    scene = shelf_scene
    link = PartAnalog.objects.get()

    response = scene["client"].post(reverse("part_analog_unlink", args=[link.pk]))

    assert response.status_code == 302
    assert not PartAnalog.objects.exists()
    assert PartType.objects.filter(pk=scene["analog"].pk).exists()


def test_opening_the_add_screen_changes_nothing(shelf_scene):
    scene = shelf_scene
    before = PartType.objects.count(), PartAnalog.objects.count()
    scene["client"].get(reverse("part_analog_add", args=[scene["original"].pk]))
    assert (PartType.objects.count(), PartAnalog.objects.count()) == before


def test_unlinking_is_refused_over_a_plain_link(shelf_scene):
    """Мутация только методом POST: обычная ссылка ничего не удаляет."""
    scene = shelf_scene
    link = PartAnalog.objects.get()
    response = scene["client"].get(reverse("part_analog_unlink", args=[link.pk]))
    assert response.status_code == 405
    assert PartAnalog.objects.filter(pk=link.pk).exists()


# --- Права -------------------------------------------------------------------------


def test_a_storekeeper_cannot_add_an_analog(client, make_user, db):
    original = create_manual_part(name="Поршень BRP", article=SAME)
    candidate = create_manual_part(name="Поршень XYZ", article=SAME)
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)

    assert client.get(reverse("part_analog_add", args=[original.pk])).status_code == 403
    response = client.post(
        reverse("part_analog_add", args=[original.pk]), {"link_part": candidate.pk}
    )
    assert response.status_code == 403
    assert not PartAnalog.objects.exists()


def test_a_storekeeper_cannot_remove_a_link(client, make_user, db):
    original = create_manual_part(name="Поршень BRP", article=SAME)
    candidate = create_manual_part(name="Поршень XYZ", article=SAME)
    link, _ = link_analog(original=original, analog=candidate)
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)

    assert client.post(reverse("part_analog_unlink", args=[link.pk])).status_code == 403
    assert PartAnalog.objects.filter(pk=link.pk).exists()


def test_a_storekeeper_still_sees_the_analogs(client, make_user, shelf_scene):
    """Смотреть можно всем рабочим ролям: это часть ответа у прилавка."""
    scene = shelf_scene
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)

    body = client.get(reverse("part_detail", args=[scene["original"].pk])).content.decode()
    assert "Поршень XYZ" in body
    assert "Убрать связь" not in body


def test_an_anonymous_visitor_is_not_let_in(shelf_scene):
    """Отдельный клиент: тот, что в сценарии, уже вошёл под руководителем."""
    from django.test import Client

    scene = shelf_scene
    stranger = Client()
    response = stranger.get(reverse("part_analog_add", args=[scene["original"].pk]))
    assert response.status_code in (302, 403)
    assert stranger.post(
        reverse("part_analog_add", args=[scene["original"].pk]),
        {"link_part": scene["analog"].pk},
    ).status_code in (302, 403)


# --- Остатки у каждой детали свои ----------------------------------------------------


def test_the_original_does_not_borrow_the_analog_stock(shelf_scene):
    """Самый опасный из возможных обманов: показать чужой остаток как свой."""
    scene = shelf_scene
    original_balance = StockBalance.objects.filter(part_type=scene["original"]).count()
    assert original_balance == 0

    from apps.core.part_lookup import resolve_part_lookup

    found = resolve_part_lookup(SAME, allow_partial=True, allow_name=True)
    by_name = {c.part.name: c for c in found.candidates}
    assert by_name["Поршень BRP"].available == Decimal("0")
    assert by_name["Поршень XYZ"].available == Decimal("4")


def test_the_analog_keeps_its_own_minimum_stock(shelf_scene):
    scene = shelf_scene
    scene["original"].min_stock_level = Decimal("10")
    scene["original"].save(update_fields=["min_stock_level"])
    scene["analog"].refresh_from_db()
    assert scene["analog"].min_stock_level == Decimal("0")


def test_linking_never_moves_a_single_unit(shelf_scene):
    scene = shelf_scene
    before = _stock_snapshot()
    extra = create_manual_part(name="Поршень QQQ", article=SAME)
    link_analog(original=scene["original"], analog=extra)
    assert _stock_snapshot() == before
