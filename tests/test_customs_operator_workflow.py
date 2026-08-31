"""Договор оператора на форме таможенных данных.

Раньше форма спрашивала одиннадцать полей, и девять из них DenisStock знал о
детали сам: английское описание и оптовую цену из каталога поставщика,
производителя по связи карточки, служебные поля источника веса. Оператор
переписывал их руками.

Здесь закреплено новое: вручную вводится только то, чего система объективно не
знает - русское таможенное название с явным подтверждением, два веса и область
применения. Остальное подставляется из канонических источников, а чего нет,
то не выдумывается: строка остаётся незаполненной и выгрузку не проходит.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.actions.models import PartCustomsInfo
from apps.actions.services import (
    CUSTOMS_COUNTRY,
    catalog_customs_usd,
    catalog_english_name,
    historical_customs_rows,
    part_export_data,
    perform_action,
    system_customs_facts,
)
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
AREA = PartCustomsInfo.ApplicationArea


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
    return make_user("boss", is_superuser=True)


def _lot(part, location, supplier, admin, qty="10"):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(qty), unit_cost_currency=Decimal("100")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(qty))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def env(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    return {"supplier": supplier, "location": location, "admin": admin}


def _brp_part(env, *, material="417127016", desc="ROLLER PULLEY", wholesale="20"):
    catalog = BrpCatalogPart.objects.create(
        material_no=material,
        part_desc=desc,
        retail_price_usd=Decimal("25.99"),
        wholesale_price_usd=Decimal(wholesale) if wholesale is not None else None,
    )
    part = promote_to_warehouse(catalog, by=env["admin"])
    _lot(part, env["location"], env["supplier"], env["admin"])
    return part, catalog


def _plain_part(env, *, name="Деталь склада", number="700100"):
    part = PartType.objects.create(
        name=name,
        category=Category.objects.create(name=f"Категория {number}"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM)
    _lot(part, env["location"], env["supplier"], env["admin"])
    return part


def _sell(env, part, quantity="1"):
    return perform_action(
        part=part,
        location=env["location"],
        action_type="sale",
        quantity=quantity,
        customer_comment="Иванов",
        by=env["admin"],
    )


def _login(client, user):
    client.login(username=user.username, password=PASSWORD)


def _form(client, part, **params):
    return client.get(reverse("actions_customs_edit", args=[part.pk]), params)


def _save(client, part, *, params=None, **payload):
    url = reverse("actions_customs_edit", args=[part.pk])
    if params:
        from urllib.parse import urlencode

        url = f"{url}?{urlencode(params)}"
    return client.post(url, payload)


def _complete(**overrides):
    payload = {
        "customs_name_ru": "РОЛИК ШКИВА",
        "customs_name_ru_confirmed": "1",
        "gross_weight_kg": "0.5",
        "net_weight_kg": "0.4",
        "application_area": AREA.SNOWMOBILE,
    }
    payload.update(overrides)
    return payload


# --- Форма: что вообще можно править -----------------------------------------


def test_the_form_offers_exactly_the_five_manual_inputs(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    html = _form(client, part).content.decode()

    import re

    form = html.split('<form method="post" class="form">', 1)[1].split("</form>", 1)[0]
    names = set(re.findall(r'name="([a-z_]+)"', form))
    editable = names - {"csrfmiddlewaretoken", "next", "flow"}
    editable -= {name for name in names if name.endswith("_shown")}
    assert editable == {
        "customs_name_ru",
        "customs_name_ru_confirmed",
        "gross_weight_kg",
        "net_weight_kg",
        "application_area",
    }, sorted(editable)


@pytest.mark.parametrize(
    "field",
    [
        "customs_name_en",
        "manufacturer",
        "country_of_origin",
        "customs_unit_price_usd",
        "weight_source_url",
        "weight_source_note",
        "source_reference",
        "weight_verified",
    ],
)
def test_the_form_has_no_input_for_a_system_or_service_field(client, env, admin, field):
    _login(client, admin)
    part, _ = _brp_part(env)

    html = _form(client, part).content.decode()

    assert f'name="{field}"' not in html, f"поле «{field}» снова спрашивают у оператора"


def test_the_system_block_shows_the_values_without_editing_them(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    html = _form(client, part).content.decode()

    assert "Данные из системы" in html
    assert "ROLLER PULLEY" in html
    assert "BRP" in html
    assert CUSTOMS_COUNTRY in html


def test_a_posted_system_field_is_ignored(client, env, admin):
    """Подделанный POST не должен править то, что оператор не редактирует."""
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(
        client,
        part,
        **_complete(
            customs_name_en="ПОДДЕЛКА",
            manufacturer="ЛЕВЫЙ ЗАВОД",
            country_of_origin="КИТАЙ",
            customs_unit_price_usd="999",
        ),
    )

    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.customs_name_en == "ROLLER PULLEY"
    assert customs.manufacturer == "BRP"
    assert customs.country_of_origin == CUSTOMS_COUNTRY
    assert customs.customs_unit_price_usd == Decimal("20")


# --- Автоматические источники -------------------------------------------------


def test_the_country_is_canada_without_the_operator(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(client, part, **_complete())

    assert PartCustomsInfo.objects.get(part_type=part).country_of_origin == "КАНАДА"
    assert system_customs_facts(part)["country_of_origin"] == "КАНАДА"


def test_the_english_name_comes_from_the_supplier_catalogue(env, admin):
    part, _ = _brp_part(env, desc="BELT DRIVE")

    assert catalog_english_name(part) == "BELT DRIVE"


def test_a_part_without_a_catalogue_has_no_english_name(env, admin):
    """Придумывать английское название неоткуда: строка останется неполной."""
    part = _plain_part(env)

    assert catalog_english_name(part) == ""
    assert system_customs_facts(part)["customs_name_en"] == ""


def test_the_manufacturer_comes_from_the_card_link(env, admin):
    part, _ = _brp_part(env)

    assert system_customs_facts(part)["manufacturer"] == "BRP"


def test_the_usd_price_is_the_catalogue_wholesale(env, admin):
    part, _ = _brp_part(env, wholesale="20")

    assert catalog_customs_usd(part) == Decimal("20")


def test_a_part_without_a_wholesale_price_has_none(env, admin):
    part, _ = _brp_part(env, wholesale=None)

    assert catalog_customs_usd(part) is None
    assert system_customs_facts(part)["customs_unit_price_usd"] is None


def test_a_missing_system_value_fails_closed(client, env, admin):
    """Нет каталога - нет английского названия и цены, строка не готова."""
    _login(client, admin)
    part = _plain_part(env)
    _sell(env, part)

    _save(client, part, **_complete())

    row = historical_customs_rows()[0]
    assert row["customs_ready"] is False
    assert "не заполнено английское название" in row["customs_missing_reasons"]
    assert "нет таможенной цены в USD" in row["customs_missing_reasons"]


# --- Подтверждение русского названия ------------------------------------------


def test_the_form_suggests_a_russian_name(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env, desc="BELT DRIVE")

    html = _form(client, part).content.decode()

    assert 'value="РЕМЕНЬ ПРИВОД"' in html or "РЕМЕНЬ" in html
    assert 'name="customs_name_ru_confirmed"' in html


def test_a_suggested_name_is_not_confirmed_by_itself(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)
    _sell(env, part)

    _save(client, part, **_complete(customs_name_ru_confirmed=""))

    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.customs_name_ru == "РОЛИК ШКИВА"
    assert customs.customs_name_ru_confirmed is False
    row = historical_customs_rows()[0]
    assert row["customs_ready"] is False
    assert "русское название не подтверждено" in row["customs_missing_reasons"]


def test_a_confirmed_name_makes_the_row_ready(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)
    _sell(env, part)

    _save(client, part, **_complete())

    row = historical_customs_rows()[0]
    assert row["customs_ready"] is True
    assert row["customs_missing_reasons"] == []
    assert row["name_ru"] == "РОЛИК ШКИВА"


def test_an_edited_name_is_stored_with_its_confirmation(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(client, part, **_complete(customs_name_ru="РОЛИК ВАРИАТОРА"))

    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.customs_name_ru == "РОЛИК ВАРИАТОРА"
    assert customs.customs_name_ru_confirmed is True


def test_changing_a_confirmed_name_drops_the_old_confirmation(client, env, admin):
    """Старое подтверждение не переезжает на новый текст."""
    _login(client, admin)
    part, _ = _brp_part(env)
    _save(client, part, **_complete(customs_name_ru="РОЛИК ШКИВА"))
    assert PartCustomsInfo.objects.get(part_type=part).customs_name_ru_confirmed is True

    _save(
        client,
        part,
        **_complete(
            customs_name_ru="СОВСЕМ ДРУГОЕ",
            customs_name_ru_shown="РОЛИК ШКИВА",
            customs_name_ru_confirmed_shown="1",
        ),
    )

    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.customs_name_ru == "СОВСЕМ ДРУГОЕ"
    assert customs.customs_name_ru_confirmed is False


def test_editing_and_confirming_in_one_step_keeps_the_confirmation(client, env, admin):
    """Правка вместе со свежей галочкой - это решение оператора, оно остаётся."""
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(
        client,
        part,
        **_complete(
            customs_name_ru="РОЛИК ВАРИАТОРА",
            customs_name_ru_shown="РОЛИК ШКИВА",
        ),
    )

    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.customs_name_ru == "РОЛИК ВАРИАТОРА"
    assert customs.customs_name_ru_confirmed is True


def test_an_empty_name_cannot_be_confirmed(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(client, part, **_complete(customs_name_ru=""))

    assert PartCustomsInfo.objects.get(part_type=part).customs_name_ru_confirmed is False


# --- Веса ---------------------------------------------------------------------


def test_valid_weights_are_saved(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(client, part, **_complete(gross_weight_kg="0,750", net_weight_kg="0.5"))

    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.gross_weight_kg == Decimal("0.750")
    assert customs.net_weight_kg == Decimal("0.5")


@pytest.mark.parametrize("value", ["abc", "-1", "0", "1.2345"])
def test_an_invalid_weight_is_refused(client, env, admin, value):
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(client, part, **_complete(gross_weight_kg=value))

    customs = PartCustomsInfo.objects.filter(part_type=part).first()
    assert customs is None or customs.gross_weight_kg is None


def test_net_above_gross_is_refused(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(client, part, **_complete(gross_weight_kg="0.4", net_weight_kg="0.9"))

    customs = PartCustomsInfo.objects.filter(part_type=part).first()
    assert customs is None or customs.gross_weight_kg is None


def test_a_manual_pair_marks_the_weight_as_operator_provided(client, env, admin):
    from apps.actions.services import MANUAL_WEIGHT_NOTE

    _login(client, admin)
    part, _ = _brp_part(env)

    _save(client, part, **_complete())

    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.weight_verified is True
    assert customs.weight_source_note == MANUAL_WEIGHT_NOTE
    assert customs.weight_source_url == ""  # ссылка не выдумывается


def test_an_existing_external_source_is_not_overwritten(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)
    PartCustomsInfo.objects.create(
        part_type=part,
        weight_source_url="https://example.com/spec",
        weight_source_note="страница поставщика",
    )

    _save(client, part, **_complete())

    customs = PartCustomsInfo.objects.get(part_type=part)
    assert customs.weight_source_url == "https://example.com/spec"
    assert customs.weight_source_note == "страница поставщика"


# --- Область применения -------------------------------------------------------


def test_the_scope_stays_a_controlled_list(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    html = _form(client, part).content.decode()
    assert '<select id="application-area"' in html
    for _value, label in AREA.choices:
        assert f">{label}</option>" in html

    _save(client, part, **_complete(application_area="ЧТО УГОДНО"))
    assert not PartCustomsInfo.objects.filter(part_type=part, application_area="ЧТО УГОДНО")


# --- Готовность ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("gross_weight_kg", "нет веса брутто"),
        ("net_weight_kg", "нет веса нетто"),
        ("application_area", "не определена область применения"),
        ("customs_name_ru_confirmed", "русское название не подтверждено"),
    ],
)
def test_a_missing_manual_value_blocks_the_row(client, env, admin, missing, reason):
    _login(client, admin)
    part, _ = _brp_part(env)
    _sell(env, part)

    _save(client, part, **_complete(**{missing: ""}))

    row = historical_customs_rows()[0]
    assert row["customs_ready"] is False
    assert reason in row["customs_missing_reasons"]


def test_the_quick_report_readiness_agrees_with_the_form(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    _save(client, part, **_complete())

    assert part_export_data(part)["customs_ready"] is True
    assert part_export_data(part)["name_ru_confirmed"] is True


# --- Последовательный обход ---------------------------------------------------


def test_saving_opens_the_next_unresolved_part(client, env, admin):
    _login(client, admin)
    first, _ = _brp_part(env, material="417127016", desc="ROLLER PULLEY")
    second, _ = _brp_part(env, material="517127016", desc="BELT DRIVE")
    _sell(env, first)
    _sell(env, second)
    report = reverse("actions_report")

    response = _save(
        client,
        first,
        params={"flow": "customs", "next": report},
        **_complete(next=report, flow="customs"),
    )

    assert response.status_code == 302
    following = reverse("actions_customs_edit", args=[second.pk])
    assert response["Location"].startswith(following), response["Location"]
    assert "flow=customs" in response["Location"]


def test_the_last_unresolved_part_returns_to_the_report(client, env, admin):
    _login(client, admin)
    only, _ = _brp_part(env)
    _sell(env, only)
    report = reverse("actions_report")

    response = _save(
        client,
        only,
        params={"flow": "customs", "next": report},
        **_complete(next=report, flow="customs"),
    )

    assert response.status_code == 302
    assert response["Location"] == report


def test_a_foreign_next_never_leaves_the_site(client, env, admin):
    _login(client, admin)
    part, _ = _brp_part(env)

    response = _save(
        client, part, **_complete(next="https://example.org/phish", flow="customs")
    )

    assert response.status_code == 302
    assert "example.org" not in response["Location"]


def test_opening_outside_the_sequence_returns_the_usual_way(client, env, admin):
    """Карточка, открытая не из обхода, возвращает туда, откуда пришли."""
    _login(client, admin)
    part, _ = _brp_part(env)
    back = reverse("actions_report") + "?q=%D0%98%D0%B2%D0%B0%D0%BD"

    response = _save(client, part, **_complete(next=back))

    assert response.status_code == 302
    assert response["Location"] == back


# --- Права --------------------------------------------------------------------


def test_a_user_without_access_is_refused(client, env, make_user):
    stranger = make_user("naблюдатель", role=roles.VIEWER)
    part, _ = _brp_part(env)

    _login(client, stranger)
    response = client.get(reverse("actions_customs_edit", args=[part.pk]))

    assert response.status_code in (302, 403)
