"""Таможенная выгрузка: вся история продаж и ремонтов, без обязательных полей.

Здесь закреплены два продуктовых решения и один инвариант.

Решение первое: незаполненные таможенные данные выгрузку НЕ отменяют.
Сотрудник вправе скачать «Форму для заказа» в любой момент; неизвестное поле
уходит пустой ячейкой и дозаполняется в самом Excel. Выдуманное значение в
декларации хуже пустой клетки, а потерянная строка хуже обоих.

Решение второе: вселенная выгрузки - это канонические строки документов,
те же самые, по которым считается отчёт «Продажи и ремонты». Раньше выгрузка
строилась по складскому журналу и умела назвать деталь только снимком сканера,
поэтому продажа или ремонт, оформленные обычным документом, в Excel не
попадали вовсе. Отсюда и расхождение итогов, которое видели раньше.

Инвариант: за всё время количество и клиентская сумма выгрузки обязаны
совпасть с «Продажами и ремонтами» до последней копейки - и до свёртки строк,
и после неё.
"""
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.actions.models import PartCustomsDataVersion, PartCustomsInfo
from apps.actions.services import (
    customs_export_reconciliation,
    historical_customs_rows,
    perform_action,
)
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.services import (
    create_stock_lot,
    receive_stock_lot,
    write_off_stock_lot_quantity,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    cancel_repair_line_quantity,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import Period, get_clients_sales_and_repairs
from apps.returns.models import StockReturnLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.services import (
    add_stock_lot_to_sale,
    cancel_sale,
    cancel_sale_line_quantity,
    complete_sale,
    create_sale,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from tests.customs_support import legacy_customs_completion

PASSWORD = "parol-12345"
SHEET = "Лист1"
DATA_ROW = 10
ALL_TIME = Period(None, None, "all")
ApplicationArea = PartCustomsInfo.ApplicationArea


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
    supplier, _ = Supplier.objects.get_or_create(name="ООО Поставка")
    location, _ = StorageLocation.objects.get_or_create(
        code="S01-D01-C01",
        defaults={"name": "Ячейка", "storage_allowed": True, "is_active": True},
    )
    other, _ = StorageLocation.objects.get_or_create(
        code="S02-D02-C02",
        defaults={"name": "Вторая", "storage_allowed": True, "is_active": True},
    )
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    return {"admin": admin, "sup": supplier, "loc": location, "loc2": other, "cat": category}


def _part(env, *, number, name="ДЕТАЛЬ", price="1000"):
    part = PartType.objects.create(
        name=name, category=env["cat"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal(price),
    )
    PartNumber.objects.create(
        part=part, value=number, kind=PartNumber.Kind.OEM, is_primary=True
    )
    return part


def _receive(env, part, quantity="50", unit_cost="100", location=None):
    batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, location or env["loc"], Decimal(quantity))
    receive_stock_lot(lot, by=env["admin"])
    return lot


def _card(part, **overrides):
    values = {
        "customs_name_ru": "РЕМЕНЬ", "customs_name_ru_confirmed": True,
        "customs_name_en": "BELT", "manufacturer": "BRP",
        "country_of_origin": "CANADA",
        "gross_weight_kg": Decimal("0.350"), "net_weight_kg": Decimal("0.300"),
        "customs_unit_price_usd": Decimal("12.50"),
        "application_area": ApplicationArea.SNOWMOBILE,
    }
    values.update(overrides)
    card = PartCustomsInfo.objects.filter(part_type=part).first()
    if card is None:
        return PartCustomsInfo.objects.create(part_type=part, **values)
    for field, value in values.items():
        setattr(card, field, value)
    card.save()
    return card


def _scanner_sale(env, part, *, quantity="1", number="", customer="Иванов"):
    # Историческая операция: веса и применимости у неё может не быть.
    # Карточка дозаполняется только на время проведения.
    with legacy_customs_completion(part):
        return perform_action(
            part=part, location=env["loc"], action_type="sale", quantity=quantity,
            customer_comment=customer, scanned_number=number, by=env["admin"],
        )

def _scanner_repair(env, part, *, quantity="1", number="", customer="Кузнецов"):
    # Историческая операция: веса и применимости у неё может не быть.
    # Карточка дозаполняется только на время проведения.
    with legacy_customs_completion(part):
        return perform_action(
            part=part, location=env["loc"], action_type="repair", quantity=quantity,
            customer_comment=customer, scanned_number=number, by=env["admin"],
        )

def _document_sale(env, lot, *, quantity="1", price="500", customer="Петров"):
    """Продажа обычным документом: без сканера и, значит, без снимка артикула."""
    # Историческая операция: веса и применимости у неё может не быть.
    # Карточка дозаполняется только на время проведения.
    with legacy_customs_completion(lot.part_type):
        sale = create_sale(customer=None, customer_name=customer, by=env["admin"])
        add_stock_lot_to_sale(
            sale, lot, Decimal(quantity), unit_price=Decimal(price), by=env["admin"]
        )
        return complete_sale(sale, by=env["admin"])

def _document_repair(env, lot, *, quantity="1", price="700", customer="Сидоров"):
    """Ремонт обычным документом. ``price=None`` - легаси-строка без снимка цены.

    Снимок приходится обнулять после создания: черновик подставляет туда
    текущую цену каталога, а исторические строки писались до того, как поле
    вообще появилось.
    """
    # Историческая операция: веса и применимости у неё может не быть.
    # Карточка дозаполняется только на время проведения.
    with legacy_customs_completion(lot.part_type):
        order = create_repair_order(customer_name=customer, by=env["admin"])
        line = add_stock_lot_to_repair_order(
            order, lot, Decimal(quantity),
            customer_unit_price_rub=None if price is None else Decimal(price), by=env["admin"],
        )
        if price is None:
            line.customer_unit_price_rub = None
            line.save(update_fields=["customer_unit_price_rub"])
        return complete_repair_order(order, by=env["admin"])

def _return_sale(env, sale, quantity, *, restock=None, to_location=None):
    line = sale.lines.first()
    document = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        document, line, Decimal(quantity),
        to_location=to_location or line.stock_lot.location,
        restock_status=restock or StockReturnLine.RestockStatus.AVAILABLE,
        by=env["admin"],
    )
    return complete_return(document, by=env["admin"])


def _login(client, make_user, *, role=None, name="boss"):
    if name != "boss":
        make_user(name, role=role)
    client.login(username=name, password=PASSWORD)


def _sheet(content: bytes):
    return openpyxl.load_workbook(BytesIO(content))[SHEET]


def _report_totals():
    rows = get_clients_sales_and_repairs(ALL_TIME)
    return (
        sum((row["sale_quantity"] + row["repair_quantity"] for row in rows), Decimal("0")),
        sum((row["client_total_known"] for row in rows), Decimal("0")),
    )


def _rows_in(sheet, count):
    return [
        {column: sheet[f"{column}{DATA_ROW + offset}"].value for column in "BCDEFGHJKM"}
        for offset in range(count)
    ]


# --- 1-5. Неполные таможенные данные больше не отменяют выгрузку -----------


def test_export_works_when_customs_data_is_complete(client, env, make_user):
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    _scanner_sale(env, part, quantity="2", number="219800345")
    _login(client, make_user)

    response = client.get(reverse("actions_export"))

    assert response.status_code == 200
    sheet = _sheet(response.content)
    assert sheet[f"B{DATA_ROW}"].value == "219800345"
    assert sheet[f"C{DATA_ROW}"].value == "РЕМЕНЬ"
    assert Decimal(str(sheet[f"J{DATA_ROW}"].value)) == Decimal("2")


def test_export_also_works_when_customs_data_is_missing(client, env, make_user):
    part = _part(env, number="219800345")
    _receive(env, part)
    _scanner_sale(env, part, quantity="2", number="219800345")
    _login(client, make_user)

    response = client.get(reverse("actions_export"))

    assert response.status_code == 200
    assert response["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )


def test_missing_customs_fields_become_blank_cells(client, env, make_user):
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part, gross_weight_kg=None, net_weight_kg=None, customs_unit_price_usd=None,
          country_of_origin="", application_area="")
    _scanner_sale(env, part, quantity="2", number="219800345")
    _login(client, make_user)

    sheet = _sheet(client.get(reverse("actions_export")).content)

    for column in "FGHKM":  # страна, брутто, нетто, цена, область применения
        assert sheet[f"{column}{DATA_ROW}"].value is None
    assert sheet[f"C{DATA_ROW}"].value == "РЕМЕНЬ"  # введённое сохранено как есть


def test_no_fake_or_default_customs_values_appear(client, env, make_user):
    """Ни BRP, ни CANADA, ни СНЕГОХОД из заготовки шаблона не должны просочиться."""
    part = _part(env, number="219800345")
    _receive(env, part)
    _scanner_sale(env, part, quantity="1", number="219800345")
    _login(client, make_user)

    sheet = _sheet(client.get(reverse("actions_export")).content)

    row = _rows_in(sheet, 1)[0]
    assert row["B"] == "219800345"
    assert row["J"] == Decimal("1")
    for column in "CDEFGHKM":
        assert row[column] is None
    # И ниже единственной строки данных заготовка шаблона тоже вычищена.
    assert sheet[f"E{DATA_ROW + 1}"].value is None
    assert sheet[f"F{DATA_ROW + 1}"].value is None


def test_an_incomplete_row_is_never_omitted(env):
    complete = _part(env, number="COMPLETE-1")
    incomplete = _part(env, number="INCOMPLETE-1", name="БЕЗ ДАННЫХ")
    _receive(env, complete)
    _receive(env, incomplete)
    _card(complete)
    _scanner_sale(env, complete, quantity="2", number="COMPLETE-1")
    _scanner_sale(env, incomplete, quantity="3", number="INCOMPLETE-1")

    rows = historical_customs_rows()

    assert {row["number"] for row in rows} == {"COMPLETE-1", "INCOMPLETE-1"}
    assert sum(row["quantity"] for row in rows) == Decimal("5")


# --- 6-8. Вселенная выгрузки: продажи, ремонты и документы без сканера ------


def test_a_sale_line_is_exported(env):
    part = _part(env, number="SALE-1")
    lot = _receive(env, part)
    _card(part)
    _document_sale(env, lot, quantity="4", price="250")

    rows = historical_customs_rows()

    assert len(rows) == 1
    assert rows[0]["quantity"] == Decimal("4")


def test_a_repair_issue_line_is_exported(env):
    part = _part(env, number="REPAIR-1")
    lot = _receive(env, part)
    _card(part)
    _document_repair(env, lot, quantity="3", price="800")

    rows = historical_customs_rows()

    assert len(rows) == 1
    assert rows[0]["quantity"] == Decimal("3")


def test_a_legacy_document_line_without_a_scanner_snapshot_is_exported(env):
    """Главная причина прежнего расхождения: документ без снимка сканера.

    Раньше такая строка не попадала в Excel вовсе и вдобавок блокировала
    выгрузку целиком. Теперь она выгружается, а недоказанный артикул остаётся
    пустой ячейкой: угадывать его по сегодняшней карточке нельзя.
    """
    part = _part(env, number="LEGACY-1")
    lot = _receive(env, part)
    _card(part)
    _document_sale(env, lot, quantity="6", price="150")

    rows = historical_customs_rows()

    assert len(rows) == 1
    assert rows[0]["number"] == ""  # не «LEGACY-1» из текущего каталога
    assert rows[0]["quantity"] == Decimal("6")
    assert rows[0]["name_ru"] == "РЕМЕНЬ"  # введённое оператором на месте


# --- 9-12. Отмены, возвраты и смешанное происхождение ----------------------


def test_a_full_cancellation_is_treated_like_the_report(env):
    part = _part(env, number="CANCEL-1")
    lot = _receive(env, part)
    _card(part)
    sale = _document_sale(env, lot, quantity="5", price="300")

    cancel_sale(sale, by=env["admin"], reason="Ошибка", author="Иванов И.")

    quantity, amount = _report_totals()
    assert quantity == Decimal("0")
    assert historical_customs_rows() == []
    assert customs_export_reconciliation()["totals"]["quantity"] == quantity
    assert customs_export_reconciliation()["totals"]["amount"] == amount


def test_a_partial_cancellation_is_treated_like_the_report(env):
    part = _part(env, number="PARTIAL-1")
    lot = _receive(env, part)
    _card(part)
    sale = _document_sale(env, lot, quantity="6", price="300")

    cancel_sale_line_quantity(
        sale.lines.first(), Decimal("2"), reason="Часть", author="И.", by=env["admin"]
    )

    quantity, amount = _report_totals()
    assert quantity == Decimal("4")
    assert amount == Decimal("1200.00")
    result = customs_export_reconciliation()
    assert result["totals"]["quantity"] == quantity
    assert result["totals"]["amount"] == amount


def test_a_partial_repair_cancellation_is_treated_like_the_report(env):
    part = _part(env, number="PARTIAL-REPAIR-1")
    lot = _receive(env, part)
    _card(part)
    order = _document_repair(env, lot, quantity="5", price="400")

    cancel_repair_line_quantity(
        order.lines.first(), Decimal("2"), reason="Часть", author="И.", by=env["admin"]
    )

    quantity, amount = _report_totals()
    assert quantity == Decimal("3")
    result = customs_export_reconciliation()
    assert result["totals"]["quantity"] == quantity
    assert result["totals"]["amount"] == amount


def test_a_cancelled_repair_order_is_treated_like_the_report(env):
    part = _part(env, number="REPAIR-CANCEL-1")
    lot = _receive(env, part)
    _card(part)
    order = _document_repair(env, lot, quantity="4", price="400")

    cancel_repair_order(order, by=env["admin"], reason="Отказ", author="И.")

    quantity, _amount = _report_totals()
    assert quantity == Decimal("0")
    assert historical_customs_rows() == []


def test_return_semantics_match_the_report(env):
    part = _part(env, number="RETURN-1")
    lot = _receive(env, part)
    _card(part)
    sale = _document_sale(env, lot, quantity="7", price="200")

    _return_sale(env, sale, "3")

    quantity, amount = _report_totals()
    assert quantity == Decimal("4")
    result = customs_export_reconciliation()
    assert result["totals"]["quantity"] == quantity
    assert result["totals"]["amount"] == amount


def test_a_quarantine_return_counts_exactly_like_an_ordinary_one(env):
    """Особый возврат в другую ячейку и в карантин - тот же возврат.

    Расход клиенту он гасит ровно так же: где физически лежит вернувшаяся
    деталь, к таможенной истории отношения не имеет.
    """
    part = _part(env, number="QUARANTINE-1")
    lot = _receive(env, part)
    _card(part)
    sale = _document_sale(env, lot, quantity="5", price="200")

    _return_sale(
        env, sale, "2", restock=StockReturnLine.RestockStatus.QUARANTINE,
        to_location=env["loc2"],
    )

    quantity, amount = _report_totals()
    assert quantity == Decimal("3")
    result = customs_export_reconciliation()
    assert result["totals"]["quantity"] == quantity
    assert result["totals"]["amount"] == amount


def test_mixed_provenance_keeps_both_kinds_of_row(env):
    part = _part(env, number="MIXED-1")
    lot = _receive(env, part)
    _card(part)
    _scanner_sale(env, part, quantity="2", number="MIXED-1")
    _document_sale(env, lot, quantity="3", price="500")

    rows = historical_customs_rows()

    assert {row["number"] for row in rows} == {"MIXED-1", ""}
    assert sum(row["quantity"] for row in rows) == Decimal("5")


def test_a_write_off_is_not_part_of_the_customs_universe(env):
    part = _part(env, number="WRITEOFF-1")
    lot = _receive(env, part)
    _card(part)
    write_off_stock_lot_quantity(lot, Decimal("3"), by=env["admin"], comment="Брак")

    quantity, _amount = _report_totals()
    assert quantity == Decimal("0")  # списания нет и в «Продажах и ремонтах»
    assert historical_customs_rows() == []


# --- 13-15. Сверка за всё время --------------------------------------------


@pytest.fixture
def whole_history(env):
    """История, в которой встречается всё, что вообще бывает."""
    made = {}
    scanner = _part(env, number="SCAN-1", price="1000")
    _card(scanner)
    _receive(env, scanner)
    made["scanner_sale"] = _scanner_sale(env, scanner, quantity="3", number="SCAN-1")

    documented = _part(env, number="DOC-1", price="2000")
    _card(documented)
    made["doc_sale"] = _document_sale(env, _receive(env, documented), quantity="4", price="2000")

    repaired = _part(env, number="REP-1", price="3000")
    _card(repaired)
    _receive(env, repaired)
    made["scanner_repair"] = _scanner_repair(env, repaired, quantity="2", number="REP-1")

    doc_repaired = _part(env, number="REP-2", price="4000")
    _card(doc_repaired)
    made["doc_repair"] = _document_repair(
        env, _receive(env, doc_repaired), quantity="5", price="4000"
    )

    scrapped = _part(env, number="SCRAP-1")
    _card(scrapped)
    write_off_stock_lot_quantity(
        _receive(env, scrapped), Decimal("2"), by=env["admin"], comment="Брак"
    )

    cancelled = _part(env, number="CANC-1", price="600")
    _card(cancelled)
    cancel_sale(
        _document_sale(env, _receive(env, cancelled), quantity="3", price="600"),
        by=env["admin"], reason="Ошибка", author="И.",
    )

    partial = _part(env, number="PART-1", price="700")
    _card(partial)
    sale = _document_sale(env, _receive(env, partial), quantity="6", price="700")
    cancel_sale_line_quantity(
        sale.lines.first(), Decimal("2"), reason="Часть", author="И.", by=env["admin"]
    )

    naked = _part(env, number="NAKED-1", price="900")  # без таможенной карточки
    made["naked_sale"] = _document_sale(
        env, _receive(env, naked), quantity="7", price="900"
    )

    unpriced = _part(env, number="UNPRICED-1", price="0")
    _card(unpriced)
    made["unpriced_repair"] = _document_repair(
        env, _receive(env, unpriced), quantity="2", price=None, customer="Легаси"
    )
    return made


def test_all_time_quantity_reconciles_with_the_report(env, whole_history):
    quantity, _amount = _report_totals()
    result = customs_export_reconciliation()

    assert result["totals"]["quantity"] == quantity
    assert result["delta"]["quantity"] == Decimal("0")


def test_all_time_money_reconciles_with_the_report(env, whole_history):
    _quantity, amount = _report_totals()
    result = customs_export_reconciliation()

    assert result["totals"]["amount"] == amount
    assert result["delta"]["amount"] == Decimal("0.00")
    assert isinstance(result["totals"]["amount"], Decimal)  # никаких float


def test_a_line_without_a_historical_price_is_unknown_on_both_sides(env, whole_history):
    """Строка без снимка цены не считается ни здесь, ни в отчёте - и это одно решение.

    Количество она даёт, деньги - нет. Придумать ей сумму нельзя, а посчитать
    её нулём значило бы занизить итог отчёта на ту же величину.
    """
    rows = get_clients_sales_and_repairs(ALL_TIME)
    assert any(row["client_total_unknown"] for row in rows)

    result = customs_export_reconciliation()
    assert len(result["price_unknown"]) == 1
    assert sum(line["quantity"] for line in result["price_unknown"]) == Decimal("2")
    assert result["delta"] == {"quantity": Decimal("0"), "amount": Decimal("0.00")}


def test_grouping_into_xlsx_rows_does_not_change_totals(env, whole_history):
    result = customs_export_reconciliation()

    assert result["totals"]["quantity"] == result["totals"]["row_quantity"]
    assert result["silent"] == []
    assert result["duplicates"] == []
    assert sum(row["quantity"] for row in result["rows"]) == result["totals"]["quantity"]


def test_no_canonical_line_disappears_silently(env, whole_history):
    result = customs_export_reconciliation()
    effective = [line for line in result["lines"] if line["quantity"] > 0]

    keys = {
        (
            line["part_id"],
            line["version"].pk if line["version"] is not None else None,
            line["number"],
        )
        for line in effective
    }
    assert keys == {row["source_key"] for row in result["rows"]}


def test_the_management_command_reports_a_zero_delta(env, whole_history, capsys):
    from django.core.management import call_command

    call_command("customs_reconcile")

    output = capsys.readouterr().out
    assert "delta_quantity: 0" in output
    assert "delta_amount: 0.00" in output
    assert "report_only_lines: 0" in output
    assert "customs_only_lines: 0" in output
    assert "RECONCILED" in output


def test_the_export_covers_exactly_the_report_lines_by_name(env, whole_history):
    """Совпадения итогов мало: состав строк тоже обязан совпасть поимённо.

    Две ошибки могут погасить друг друга в сумме. Поэтому набор строк выгрузки
    сверяется с набором строк отчёта отдельным запросом, а не «по построению».
    """
    from apps.actions.management.commands.customs_reconcile import _report_line_keys

    customs_keys = {
        (line["kind"], line["line_id"])
        for line in customs_export_reconciliation()["lines"]
    }

    assert customs_keys == _report_line_keys()


def test_a_line_the_report_drops_is_dropped_by_the_export_too(env, whole_history):
    """Отменённый документ уходит из обоих наборов разом, а не из одного."""
    from apps.actions.management.commands.customs_reconcile import _report_line_keys

    sale = whole_history["doc_sale"]
    line_key = ("sale", sale.lines.first().pk)
    assert line_key in _report_line_keys()

    cancel_sale(sale, by=env["admin"], reason="Ошибка", author="И.")

    customs_keys = {
        (line["kind"], line["line_id"])
        for line in customs_export_reconciliation()["lines"]
    }
    assert line_key not in _report_line_keys()
    assert line_key not in customs_keys
    assert customs_keys == _report_line_keys()


def test_the_command_breaks_the_history_down_by_sales_and_repairs(
    env, whole_history, capsys
):
    from django.core.management import call_command

    call_command("customs_reconcile")

    output = dict(
        line.split(": ", 1) for line in capsys.readouterr().out.splitlines()
        if ": " in line
    )
    assert int(output["sales_lines"]) + int(output["repair_lines"]) == int(
        output["canonical_line_count"]
    )
    assert (
        Decimal(output["sales_quantity"]) + Decimal(output["repair_quantity"])
        == Decimal(output["export_quantity"])
    )
    assert (
        Decimal(output["sales_amount"]) + Decimal(output["repair_amount"])
        == Decimal(output["export_amount"])
    )
    assert int(output["sales_documents"]) >= 1
    assert int(output["repair_documents"]) >= 1
    assert int(output["blank_article_rows"]) >= 1


# --- 16-18. Фильтры, права и сам файл ---------------------------------------


def test_period_and_type_filters_still_work(client, env, make_user):
    import datetime

    from apps.sales.models import Sale

    sold = _part(env, number="FILTER-SALE")
    issued = _part(env, number="FILTER-REPAIR")
    _card(sold)
    _card(issued)
    sale = _document_sale(env, _receive(env, sold), quantity="2", price="100")
    _document_repair(env, _receive(env, issued), quantity="3", price="100")

    assert len(historical_customs_rows()) == 2
    assert len(historical_customs_rows(action_type="sale")) == 1
    assert len(historical_customs_rows(action_type="repair")) == 1
    assert historical_customs_rows(action_type="reserve") == []

    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    Sale.objects.filter(pk=sale.pk).update(
        sold_at=sale.sold_at - datetime.timedelta(days=2)
    )
    assert len(historical_customs_rows(date_from=yesterday)) == 1

    assert len(historical_customs_rows(part_number="FILTER-SALE")) == 1
    assert len(historical_customs_rows(location_code="S01-D01")) == 2
    assert historical_customs_rows(location_code="S02-D02") == []
    assert len(historical_customs_rows(q="Петров")) == 1
    assert historical_customs_rows(q="Никого") == []


def test_permissions_are_still_enforced(client, env, make_user):
    part = _part(env, number="PERM-1")
    _receive(env, part)
    _card(part)
    _scanner_sale(env, part, quantity="1", number="PERM-1")

    assert client.get(reverse("actions_export")).status_code == 302  # аноним на логин

    _login(client, make_user, role=roles.VIEWER, name="viewer")
    assert client.get(reverse("actions_export")).status_code == 403

    client.logout()
    _login(client, make_user, role=roles.SELLER, name="seller")
    # Продавец проводит действия, но оптовую цену в USD видеть не вправе.
    assert client.get(reverse("actions_export")).status_code == 403


def test_the_workbook_opens_with_the_expected_headers_and_rows(client, env, make_user):
    first = _part(env, number="AAA-1")
    second = _part(env, number="BBB-2", name="ВТОРАЯ")
    _receive(env, first)
    _receive(env, second)
    _card(first)
    _card(second, customs_name_ru="ФИЛЬТР", customs_name_en="FILTER")
    _scanner_sale(env, first, quantity="2", number="AAA-1")
    _scanner_sale(env, second, quantity="3", number="BBB-2")
    _login(client, make_user)

    sheet = _sheet(client.get(reverse("actions_export")).content)

    assert [row["B"] for row in _rows_in(sheet, 2)] == ["AAA-1", "BBB-2"]
    assert [row["C"] for row in _rows_in(sheet, 2)] == ["РЕМЕНЬ", "ФИЛЬТР"]
    assert [row["J"] for row in _rows_in(sheet, 2)] == [Decimal("2"), Decimal("3")]
    assert sheet[f"I{DATA_ROW}"].value == f"=J{DATA_ROW}*G{DATA_ROW}"
    assert sheet[f"L{DATA_ROW}"].value == f"=K{DATA_ROW}*J{DATA_ROW}"


def test_the_export_writes_nothing_to_the_database(client, env, make_user):
    part = _part(env, number="READONLY-1")
    lot = _receive(env, part)
    _document_sale(env, lot, quantity="2", price="100")
    _login(client, make_user)
    before = (
        PartCustomsInfo.objects.count(),
        PartCustomsDataVersion.objects.count(),
    )

    assert client.get(reverse("actions_export")).status_code == 200

    assert (
        PartCustomsInfo.objects.count(),
        PartCustomsDataVersion.objects.count(),
    ) == before


def test_a_filter_cannot_make_an_unpriced_repair_order_look_priced(env):
    """Фильтр сужает строки, но не переписывает правило «сумма заказа неизвестна»."""
    priced = _part(env, number="ORDER-PRICED", price="500")
    unpriced = _part(env, number="ORDER-UNPRICED", price="0")
    _card(priced)
    _card(unpriced)
    priced_lot = _receive(env, priced)
    unpriced_lot = _receive(env, unpriced)
    order = create_repair_order(customer_name="Легаси", by=env["admin"])
    add_stock_lot_to_repair_order(
        order, priced_lot, Decimal("2"),
        customer_unit_price_rub=Decimal("500"), by=env["admin"],
    )
    line = add_stock_lot_to_repair_order(
        order, unpriced_lot, Decimal("1"), customer_unit_price_rub=None, by=env["admin"]
    )
    line.customer_unit_price_rub = None
    line.save(update_fields=["customer_unit_price_rub"])
    complete_repair_order(order, by=env["admin"])

    # Фильтр оставляет только строку с ценой - но заказ всё равно неизвестен.
    narrowed = customs_export_reconciliation(part_number="ORDER-PRICED")
    assert len(narrowed["lines"]) == 1
    assert narrowed["lines"][0]["amount_known"] is False
    assert narrowed["totals"]["amount"] == Decimal("0.00")


def test_the_report_page_counts_incomplete_positions_itself(client, env, make_user):
    """Число в предупреждении вычисляется, а не записано в шаблоне."""
    complete = _part(env, number="UI-COMPLETE")
    first = _part(env, number="UI-MISSING-1", name="ПЕРВАЯ")
    second = _part(env, number="UI-MISSING-2", name="ВТОРАЯ")
    for part in (complete, first, second):
        _receive(env, part)
    _card(complete)
    _scanner_sale(env, complete, quantity="1", number="UI-COMPLETE")
    _scanner_sale(env, first, quantity="1", number="UI-MISSING-1")
    _login(client, make_user)

    html = client.get(reverse("actions_report")).content.decode()
    assert "У 1 позиций таможенные данные заполнены не" in html

    _scanner_sale(env, second, quantity="1", number="UI-MISSING-2")
    html = client.get(reverse("actions_report")).content.decode()
    assert "У 2 позиций таможенные данные заполнены не" in html
    assert "Экспорт в Excel для таможни" in html


def test_the_report_page_names_rows_whose_article_is_unproven(client, env, make_user):
    part = _part(env, number="UI-ARTICLE")
    lot = _receive(env, part)
    _card(part)
    _document_sale(env, lot, quantity="2", price="100")
    _login(client, make_user)

    html = client.get(reverse("actions_report")).content.decode()

    assert "У 1 позиций не сохранён артикул" in html
    assert "Экспорт в Excel для таможни" in html


def test_every_canonical_row_is_written_even_beyond_the_template_limit(
    client, env, make_user
):
    """Шаблон рассчитан на 140 строк: истории это не ограничение."""
    lot = None
    for index in range(150):
        part = _part(env, number=f"BULK-{index:03d}", name=f"ДЕТАЛЬ {index}")
        lot = _receive(env, part, quantity="5")
        _document_sale(env, lot, quantity="1", price="100")
    _login(client, make_user)

    sheet = _sheet(client.get(reverse("actions_export")).content)

    written = [
        sheet[f"J{DATA_ROW + offset}"].value for offset in range(150)
    ]
    assert all(value is not None for value in written)
    assert sum(Decimal(str(value)) for value in written) == Decimal("150")
    assert customs_export_reconciliation()["totals"]["quantity"] == Decimal("150")
