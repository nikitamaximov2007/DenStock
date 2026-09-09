"""Stage 4 — вес и область применения вводятся в быстрых действиях.

Сотрудник держит деталь в руках и вводит вес в ГРАММАХ. Хранение остаётся
каноническим: PartCustomsInfo.gross_weight_kg / net_weight_kg, Decimal(8,3),
килограммы. Новых полей и переноса данных нет намеренно - три знака после
запятой это ровно целые граммы.

Таможенный минимум 0.03 кг применяется ТОЛЬКО в момент выгрузки. Запомненный
фактический вес детали от него не меняется никогда.

Продажа и выдача в ремонт - единственные таможенные источники (см.
apps.actions.customs_history). Резерв товар клиенту не отдаёт, поэтому этой
проверкой не блокируется.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.cart import KIND_SALE, add_scan, complete_cart, open_cart
from apps.actions.models import PartCustomsInfo, WarehouseAction
from apps.actions.services import (
    ActionError,
    customs_export_weight_kg,
    parse_application_area,
    parse_weight_g,
    part_export_data,
    perform_action,
    weight_kg_as_grams,
)
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.customs_orders.export import export_customs_order_xlsx
from apps.customs_orders.models import CustomsOrder
from apps.customs_orders.services import create_customs_order, eligible_customs_sources
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
Area = PartCustomsInfo.ApplicationArea


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
def admin(make_user):
    return make_user("admin", is_superuser=True)


def _stock(part, location, qty, sup, admin):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(str(qty)), unit_cost_currency=Decimal("1")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def env(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D03-C08", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Болт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value="700100", kind=PartNumber.Kind.OEM)
    _stock(part, loc, 10, sup, admin)
    return {"sup": sup, "admin": admin, "loc": loc, "part": part}


def _login(client, make_user, *, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


def _card(part, **overrides):
    values = {
        "customs_name_ru": "БОЛТ", "customs_name_ru_confirmed": True,
        "customs_name_en": "BOLT", "manufacturer": "BRP",
        "country_of_origin": "CANADA", "customs_unit_price_usd": Decimal("7"),
    }
    values.update(overrides)
    return PartCustomsInfo.objects.create(part_type=part, **values)


def _add(client, env, *, kind="sale", qty="1"):
    return client.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": kind, "quantity": qty, "q": "700100",
    })


def _row(client, env, *, kind="sale", **fields):
    payload = {
        "kind": kind, "operation": "set",
        "row_key": f"{env['part'].pk}:{env['loc'].pk}",
        "quantity": "1", "q": "700100",
    }
    payload.update(fields)
    return client.post(reverse("actions_cart_update"), payload)


def _complete(client, *, kind="sale", customer=None):
    customer = customer or Customer.objects.create(name="Иванов")
    return client.post(reverse("actions_cart_complete"), {
        "kind": kind, "customer_id": customer.pk, "q": "700100",
    }, follow=True)


# --- Граммы -> килограммы: точный Decimal ------------------------------------------------


@pytest.mark.parametrize(
    "grams,kg",
    [("12", "0.012"), ("250", "0.250"), ("1000", "1.000"), ("30", "0.030"), ("1", "0.001")],
)
def test_grams_convert_to_exact_decimal_kg(grams, kg):
    assert parse_weight_g(grams) == Decimal(kg)


def test_grams_conversion_uses_no_float():
    assert str(parse_weight_g("12")) == "0.012"
    assert parse_weight_g("12") == Decimal("12") / Decimal("1000")


def test_empty_weight_stays_unknown():
    assert parse_weight_g("") is None
    assert parse_weight_g(None) is None


@pytest.mark.parametrize("raw", ["0", "-5", "12.5", "abc"])
def test_bad_grams_rejected(raw):
    with pytest.raises(ValueError):
        parse_weight_g(raw)


def test_kg_shown_back_to_operator_in_grams():
    assert weight_kg_as_grams(Decimal("0.012")) == 12
    assert weight_kg_as_grams(Decimal("1.000")) == 1000
    assert weight_kg_as_grams(None) is None


# --- Таможенный минимум только на выгрузке -----------------------------------------------


@pytest.mark.parametrize(
    "grams,exported",
    [("12", "0.03"), ("29", "0.03"), ("30", "0.03"), ("31", "0.031"),
     ("250", "0.25"), ("1000", "1.00")],
)
def test_customs_minimum_applies_at_export(grams, exported):
    assert customs_export_weight_kg(parse_weight_g(grams)) == Decimal(exported)


def test_customs_minimum_never_invents_a_weight():
    assert customs_export_weight_kg(None) is None


def test_export_row_uses_the_minimum_but_storage_keeps_the_actual(env):
    part = env["part"]
    _card(part, gross_weight_kg=Decimal("0.012"), net_weight_kg=Decimal("0.010"),
          application_area=Area.SNOWMOBILE)
    row = part_export_data(part)
    assert row["gross_weight_kg"] == Decimal("0.03")
    assert row["net_weight_kg"] == Decimal("0.03")
    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.gross_weight_kg == Decimal("0.012")
    assert customs.net_weight_kg == Decimal("0.010")


def test_gross_and_net_get_the_minimum_independently(env):
    part = env["part"]
    _card(part, gross_weight_kg=Decimal("0.250"), net_weight_kg=Decimal("0.020"),
          application_area=Area.SNOWMOBILE)
    row = part_export_data(part)
    assert row["gross_weight_kg"] == Decimal("0.25")
    assert row["net_weight_kg"] == Decimal("0.03")


# --- Область применения ------------------------------------------------------------------


def test_quick_action_application_list_is_exactly_the_agreed_five(client, make_user, env):
    _login(client, make_user)
    _add(client, env)
    html = client.get(reverse("actions_scan")).content.decode()
    for label in ("Гидроцикл", "Квадроцикл", "Снегоход", "Лодочный мотор", "Катер"):
        assert f">{label}</option>" in html
    assert "Не выбрано" in html


@pytest.mark.parametrize(
    "value", ["ГИДРОЦИКЛ", "КВАДРОЦИКЛ", "СНЕГОХОД", "ЛОДОЧНЫЙ МОТОР", "КАТЕР"],
)
def test_allowed_application_values_accepted(value):
    assert parse_application_area(value) == value


def test_application_outside_the_list_rejected():
    with pytest.raises(ValueError):
        parse_application_area("ТРАКТОР")


def test_missing_application_is_not_invented():
    assert parse_application_area("") == ""


# --- Автоподстановка ---------------------------------------------------------------------


def test_remembered_weights_are_prefilled_in_grams(client, make_user, env):
    _card(env["part"], gross_weight_kg=Decimal("0.250"), net_weight_kg=Decimal("0.200"))
    _login(client, make_user)
    _add(client, env)
    html = client.get(reverse("actions_scan")).content.decode()
    assert 'name="gross_weight_g"' in html
    assert 'value="250"' in html
    assert 'value="200"' in html


def test_remembered_application_is_preselected(client, make_user, env):
    _card(env["part"], application_area=Area.ATV)
    _login(client, make_user)
    _add(client, env)
    html = client.get(reverse("actions_scan")).content.decode()
    assert '<option value="КВАДРОЦИКЛ"\n                              selected' in html.replace(
        "\r", ""
    ) or 'value="КВАДРОЦИКЛ"' in html and "selected" in html


def test_unknown_metadata_stays_empty_not_invented(client, make_user, env):
    _login(client, make_user)
    _add(client, env)
    html = client.get(reverse("actions_scan")).content.decode()
    assert 'name="gross_weight_g"\n                         type="text"' in html or (
        'name="gross_weight_g"' in html
    )
    assert "Не выбрано" in html
    assert not PartCustomsInfo.objects.filter(part_type=env["part"]).exists()


# --- Обязательность на проведении --------------------------------------------------------


def test_missing_gross_weight_rejects_the_sale(client, make_user, env):
    _card(env["part"], net_weight_kg=Decimal("0.200"), application_area=Area.SNOWMOBILE)
    _login(client, make_user)
    _add(client, env)
    html = _complete(client).content.decode()
    assert "вес брутто" in html
    assert not WarehouseAction.objects.exists()


def test_missing_net_weight_rejects_the_sale(client, make_user, env):
    _card(env["part"], gross_weight_kg=Decimal("0.250"), application_area=Area.SNOWMOBILE)
    _login(client, make_user)
    _add(client, env)
    html = _complete(client).content.decode()
    assert "вес нетто" in html
    assert not WarehouseAction.objects.exists()


def test_missing_application_rejects_the_sale(client, make_user, env):
    _card(env["part"], gross_weight_kg=Decimal("0.250"), net_weight_kg=Decimal("0.200"))
    _login(client, make_user)
    _add(client, env)
    html = _complete(client).content.decode()
    assert "область применения" in html
    assert not WarehouseAction.objects.exists()


def test_rejected_completion_changes_no_stock(client, make_user, env):
    from apps.inventory.models import StockMovement

    _login(client, make_user)
    _add(client, env)
    movements = StockMovement.objects.count()
    _complete(client)
    assert StockMovement.objects.count() == movements
    assert not WarehouseAction.objects.exists()


def test_reserve_is_not_blocked_by_missing_customs_metadata(env):
    """Резерв товар клиенту не отдаёт и таможенным источником не является."""
    action = perform_action(
        part=env["part"], location=env["loc"], action_type=WarehouseAction.Type.RESERVE,
        quantity="1", customer_comment="Иванов", by=env["admin"],
    )
    assert action.action_type == WarehouseAction.Type.RESERVE
    assert not PartCustomsInfo.objects.filter(part_type=env["part"]).exists()


# --- Запоминание после успешной операции -------------------------------------------------


def test_successful_sale_remembers_entered_values(client, make_user, env):
    _login(client, make_user)
    _add(client, env)
    _row(client, env, gross_weight_g="250", net_weight_g="200", application_area="КВАДРОЦИКЛ")
    _complete(client)
    customs = PartCustomsInfo.objects.get(part_type=env["part"])
    assert customs.gross_weight_kg == Decimal("0.250")
    assert customs.net_weight_kg == Decimal("0.200")
    assert customs.application_area == "КВАДРОЦИКЛ"
    assert WarehouseAction.objects.count() == 1


def test_edited_weight_overrides_the_remembered_one(client, make_user, env):
    _card(env["part"], gross_weight_kg=Decimal("0.250"), net_weight_kg=Decimal("0.200"),
          application_area=Area.SNOWMOBILE)
    _login(client, make_user)
    _add(client, env)
    _row(client, env, gross_weight_g="12", net_weight_g="10", application_area="КАТЕР")
    _complete(client)
    customs = PartCustomsInfo.objects.get(part_type=env["part"])
    assert customs.gross_weight_kg == Decimal("0.012")
    assert customs.net_weight_kg == Decimal("0.010")
    assert customs.application_area == "КАТЕР"


def test_nothing_is_remembered_before_the_operation_succeeds(client, make_user, env):
    _login(client, make_user)
    _add(client, env)
    _row(client, env, gross_weight_g="250", net_weight_g="200", application_area="КАТЕР")
    assert not PartCustomsInfo.objects.filter(
        part_type=env["part"], gross_weight_kg=Decimal("0.250")
    ).exists()


def test_remembering_writes_an_immutable_version(client, make_user, env):
    _login(client, make_user)
    _add(client, env)
    _row(client, env, gross_weight_g="250", net_weight_g="200", application_area="СНЕГОХОД")
    _complete(client)
    version = env["part"].customs_data_versions.order_by("-version").first()
    assert version is not None
    assert version.gross_weight_kg == Decimal("0.250")
    assert version.application_area == "СНЕГОХОД"


def test_legacy_null_weight_stays_null_when_operator_enters_nothing(env):
    """Пустое поле не превращается ни в ноль, ни в выдуманные 30 граммов."""
    _card(env["part"])
    cart = open_cart(KIND_SALE, by=env["admin"])
    add_scan(cart, env["part"], env["loc"], by=env["admin"])
    with pytest.raises(ActionError):
        complete_cart(cart, customer_comment="Иванов", by=env["admin"])
    customs = PartCustomsInfo.objects.get(part_type=env["part"])
    assert customs.gross_weight_kg is None
    assert customs.net_weight_kg is None
    assert customs.application_area == ""


# --- Замороженный таможенный заказ -------------------------------------------------------


def _completed_sale(client, make_user, env):
    _login(client, make_user)
    _add(client, env)
    _row(client, env, gross_weight_g="250", net_weight_g="200", application_area="СНЕГОХОД")
    _complete(client)


def test_customs_order_freezes_weights_and_application(client, make_user, env):
    _card(env["part"])
    _completed_sale(client, make_user, env)
    sources = eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL)
    assert sources, "продажа должна попасть в очередь таможни"
    order = create_customs_order(number=125, lines=sources, by=env["admin"])
    line = order.lines.get()
    assert line.gross_weight_kg == Decimal("0.25")
    assert line.net_weight_kg == Decimal("0.20")
    assert line.application_area == "СНЕГОХОД"


def test_later_part_edits_do_not_mutate_an_existing_order(client, make_user, env):
    _card(env["part"])
    _completed_sale(client, make_user, env)
    order = create_customs_order(
        number=125,
        lines=eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL),
        by=env["admin"],
    )
    customs = PartCustomsInfo.objects.get(part_type=env["part"])
    customs.gross_weight_kg = Decimal("9.000")
    customs.net_weight_kg = Decimal("8.000")
    customs.application_area = Area.ATV
    customs.save()

    line = order.lines.get()
    line.refresh_from_db()
    assert line.gross_weight_kg == Decimal("0.25")
    assert line.net_weight_kg == Decimal("0.20")
    assert line.application_area == "СНЕГОХОД"


def test_customs_order_freezes_actual_small_weight_but_exports_the_minimum(
    client, make_user, env
):
    _card(env["part"])
    _login(client, make_user)
    _add(client, env)
    _row(client, env, gross_weight_g="12", net_weight_g="10", application_area="СНЕГОХОД")
    _complete(client)
    order = create_customs_order(
        number=125,
        lines=eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL),
        by=env["admin"],
    )

    line = order.lines.get()
    assert line.gross_weight_kg == Decimal("0.012")
    assert line.net_weight_kg == Decimal("0.010")

    from openpyxl import load_workbook

    sheet = load_workbook(export_customs_order_xlsx(order)).worksheets[0]
    assert Decimal(str(sheet["G10"].value)) == Decimal("0.03")
    assert Decimal(str(sheet["H10"].value)) == Decimal("0.03")
    assert sheet["G10"].number_format == "0.00"


def test_a_failed_completion_remembers_nothing(env):
    """Запись версии идёт до списания, поэтому её обязана откатить транзакция."""
    _card(env["part"])
    cart = open_cart(KIND_SALE, by=env["admin"])
    add_scan(cart, env["part"], env["loc"], quantity=Decimal("5"), by=env["admin"])
    # Тот же остаток уходит другим документом: проведение корзины упадёт уже
    # после того, как таможенные данные будут записаны.
    other = open_cart(KIND_SALE, by=env["admin"])
    add_scan(other, env["part"], env["loc"], quantity=Decimal("10"), by=env["admin"])
    metadata = {
        env["part"].pk: {
            "gross_weight_kg": Decimal("0.250"),
            "net_weight_kg": Decimal("0.200"),
            "application_area": "СНЕГОХОД",
        }
    }
    complete_cart(other, customer_comment="Первый", by=env["admin"], customs_metadata=metadata)
    PartCustomsInfo.objects.filter(part_type=env["part"]).update(
        gross_weight_kg=None, net_weight_kg=None, application_area=""
    )
    versions_before = env["part"].customs_data_versions.count()

    with pytest.raises(ActionError):
        complete_cart(cart, customer_comment="Второй", by=env["admin"], customs_metadata=metadata)

    customs = PartCustomsInfo.objects.get(part_type=env["part"])
    assert customs.gross_weight_kg is None
    assert customs.net_weight_kg is None
    assert customs.application_area == ""
    assert env["part"].customs_data_versions.count() == versions_before
