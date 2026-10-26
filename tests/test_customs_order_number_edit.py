"""Correcting the visible customs number must not rewrite a saved order."""

from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.db import DatabaseError, IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from apps.accounts import roles
from apps.customs_orders.models import CustomsOrder, CustomsOrderLine, CustomsOrderNumberChange

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_superuser(username="customs-admin", password="secret")


@pytest.fixture
def order(admin):
    order = CustomsOrder.objects.create(
        number=129, order_type=CustomsOrder.OrderType.ORIGINAL,
        status=CustomsOrder.Status.FINALIZED, created_by=admin, finalized_by=admin,
        fx_rate=Decimal("105.0000"), total_quantity=Decimal("3.000"),
        total_rub=Decimal("3150.00"),
    )
    CustomsOrderLine.objects.create(
        order=order, source=CustomsOrderLine.Source.SALE, source_id=1001,
        article="01.5600.000", name_ru="ДЕТАЛЬ", name_en="PART", manufacturer="BRP",
        country="CANADA", quantity=Decimal("3.000"),
        wholesale_usd=Decimal("10.0000"), rub_amount=Decimal("3150.00"),
        application_area="СНЕГОХОД",
    )
    return order


def _detail(order):
    return reverse("customs_order_detail", args=[order.pk])


def _change(order):
    return reverse("customs_order_number", args=[order.pk])


def test_renumber_finalized_order_preserves_pk_lines_and_every_other_header_field(
    client, admin, order,
):
    before = CustomsOrder.objects.filter(pk=order.pk).values().get()
    lines_before = list(CustomsOrderLine.objects.filter(order=order).values())
    client.force_login(admin)

    response = client.post(_change(order), {
        "number": "130", "fx_rate": "1", "total_rub": "0", "status": "draft",
        "article": "FORGED", "quantity": "99", "created_by": "0",
    })

    assert response.status_code == 302
    assert response.url == _detail(order)
    after = CustomsOrder.objects.filter(pk=order.pk).values().get()
    assert after.pop("number") == 130
    assert before.pop("number") == 129
    assert after == before
    assert list(CustomsOrderLine.objects.filter(order=order).values()) == lines_before
    event = CustomsOrderNumberChange.objects.get(order=order)
    assert (event.old_number, event.new_number, event.changed_by_id) == (129, 130, admin.pk)
    assert event.changed_at is not None

    detail = client.get(_detail(order)).content.decode()
    listing = client.get(reverse("customs_orders_list")).content.decode()
    assert "заказ №130" in detail
    assert "заказ №130" in listing
    assert "№129 → №130" in detail
    assert f'href="{_detail(order)}"' in listing


def test_cancel_and_read_only_pages_do_not_change_number(client, admin, order):
    client.force_login(admin)
    edit = client.get(_detail(order), {"edit_number": "1"})
    assert edit.status_code == 200
    assert 'value="129"' in edit.content.decode()
    assert f'href="{_detail(order)}">Отмена</a>' in edit.content.decode()

    cancel = client.get(_detail(order))
    assert cancel.status_code == 200
    assert "Новый номер заказа" not in cancel.content.decode()
    order.refresh_from_db()
    assert order.number == 129
    assert not CustomsOrderNumberChange.objects.exists()


def test_duplicate_is_rejected_with_entered_number_preserved(client, admin, order):
    CustomsOrder.objects.create(
        number=130, order_type=order.order_type, fx_rate=Decimal("100"),
    )
    client.force_login(admin)

    response = client.post(_change(order), {"number": "130"})

    assert response.status_code == 400
    html = response.content.decode()
    assert "Заказ с таким номером уже существует" in html
    assert 'value="130"' in html
    order.refresh_from_db()
    assert order.number == 129
    assert not CustomsOrderNumberChange.objects.exists()


@pytest.mark.parametrize(
    "value", ("", "0", "-2", "1.5", "1_0", "+130", "2147483648", "abc"),
)
def test_invalid_number_is_rejected_without_losing_input(client, admin, order, value):
    client.force_login(admin)

    response = client.post(_change(order), {"number": value})

    assert response.status_code == 400
    assert f'value="{value}"' in response.content.decode()
    order.refresh_from_db()
    assert order.number == 129
    assert not CustomsOrderNumberChange.objects.exists()


def test_same_number_is_allowed_in_other_order_type(client, admin, order):
    CustomsOrder.objects.create(
        number=130, order_type=CustomsOrder.OrderType.ANALOG, fx_rate=Decimal("100"),
    )
    client.force_login(admin)

    assert client.post(_change(order), {"number": "130"}).status_code == 302
    order.refresh_from_db()
    assert order.number == 130


def test_saved_draft_can_be_renumbered_without_finalizing(client, admin):
    draft = CustomsOrder.objects.create(
        number=129, status=CustomsOrder.Status.DRAFT, created_by=admin,
        fx_rate=Decimal("105.0000"),
    )
    original_pk = draft.pk
    client.force_login(admin)

    assert client.post(_change(draft), {"number": "130"}).status_code == 302
    draft.refresh_from_db()
    assert (draft.pk, draft.number, draft.status) == (
        original_pk, 130, CustomsOrder.Status.DRAFT,
    )
    assert CustomsOrderNumberChange.objects.get(order=draft).old_number == 129


def test_unchanged_number_does_not_create_a_false_audit_event(client, admin, order):
    client.force_login(admin)

    assert client.post(_change(order), {"number": "129"}).status_code == 302
    assert not CustomsOrderNumberChange.objects.exists()


def test_repeated_corrections_keep_the_number_chain(client, admin, order):
    client.force_login(admin)

    assert client.post(_change(order), {"number": "130"}).status_code == 302
    assert client.post(_change(order), {"number": "131"}).status_code == 302

    assert list(order.number_changes.values_list("old_number", "new_number")) == [
        (130, 131), (129, 130),
    ]


def test_manager_with_customs_access_can_edit_number(client, django_user_model, order):
    manager = django_user_model.objects.create_user(username="customs-manager", password="secret")
    manager.groups.add(Group.objects.get_or_create(name=roles.MANAGER)[0])
    client.force_login(manager)

    assert "Изменить номер" in client.get(_detail(order)).content.decode()
    assert client.post(_change(order), {"number": "130"}).status_code == 302
    assert CustomsOrderNumberChange.objects.get(order=order).changed_by_id == manager.pk


def test_viewer_cannot_edit_number(client, django_user_model, order):
    viewer = django_user_model.objects.create_user(username="customs-viewer", password="secret")
    viewer.groups.add(Group.objects.get_or_create(name=roles.VIEWER)[0])
    client.force_login(viewer)

    page = client.get(_detail(order), {"edit_number": "1"})
    assert page.status_code == 403
    assert client.post(_change(order), {"number": "130"}).status_code == 403
    order.refresh_from_db()
    assert order.number == 129


def test_anonymous_user_cannot_edit_number(client, order):
    assert client.post(_change(order), {"number": "130"}).status_code == 302
    order.refresh_from_db()
    assert order.number == 129


def test_database_error_keeps_entered_number_and_leaves_order_intact(
    client, admin, order, monkeypatch,
):
    client.force_login(admin)
    monkeypatch.setattr(
        "apps.customs_orders.views.rename_customs_order_number",
        lambda **kwargs: (_ for _ in ()).throw(DatabaseError("synthetic failure")),
    )

    response = client.post(_change(order), {"number": "130"})

    assert response.status_code == 503
    assert "Не удалось сохранить номер" in response.content.decode()
    assert 'value="130"' in response.content.decode()
    order.refresh_from_db()
    assert order.number == 129


def test_failed_audit_insert_rolls_back_number_change(client, admin, order, monkeypatch):
    client.force_login(admin)

    def fail_audit(**kwargs):
        raise IntegrityError("synthetic audit failure")

    monkeypatch.setattr(CustomsOrderNumberChange.objects, "create", fail_audit)

    response = client.post(_change(order), {"number": "130"})

    assert response.status_code == 503
    assert 'value="130"' in response.content.decode()
    order.refresh_from_db()
    assert order.number == 129
    assert not CustomsOrderNumberChange.objects.exists()


def test_export_uses_corrected_filename_without_changing_frozen_rows(client, admin, order):
    client.force_login(admin)
    before = client.get(_detail(order), {"export": "1"})
    assert before.status_code == 200
    assert before["Content-Disposition"] == 'attachment; filename="customs_order_129.xlsx"'
    old_sheet = openpyxl.load_workbook(BytesIO(before.content))["Оригиналы"]
    old_row = [old_sheet[f"{column}10"].value for column in "BCDEFGHIJKLM"]

    assert client.post(_change(order), {"number": "130"}).status_code == 302
    after = client.get(_detail(order), {"export": "1"})
    assert after.status_code == 200
    assert after["Content-Disposition"] == 'attachment; filename="customs_order_130.xlsx"'
    new_sheet = openpyxl.load_workbook(BytesIO(after.content))["Оригиналы"]
    assert [new_sheet[f"{column}10"].value for column in "BCDEFGHIJKLM"] == old_row
    assert new_sheet["B10"].value == "01.5600.000"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_additive_audit_migration_preserves_populated_order():
    previous = [("customs_orders", "0004_draft_finalized_lifecycle")]
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(previous)
        old_apps = executor.loader.project_state(previous).apps
        OldOrder = old_apps.get_model("customs_orders", "CustomsOrder")
        OldLine = old_apps.get_model("customs_orders", "CustomsOrderLine")
        order = OldOrder.objects.create(
            number=129, order_type="original", status="finalized",
            fx_rate=Decimal("105.0000"), total_quantity=Decimal("2.000"),
            total_rub=Decimal("2100.00"),
        )
        line = OldLine.objects.create(
            order_id=order.pk, source="sale", source_id=901, article="01.5600.000",
            quantity=Decimal("2.000"), wholesale_usd=Decimal("10.0000"),
            rub_amount=Decimal("2100.00"),
        )

        MigrationExecutor(connection).migrate(leaves)

        restored = CustomsOrder.objects.get(pk=order.pk)
        assert (restored.number, restored.status, restored.fx_rate) == (
            129, "finalized", Decimal("105.0000"),
        )
        assert (restored.total_quantity, restored.total_rub) == (
            Decimal("2.000"), Decimal("2100.00"),
        )
        assert CustomsOrderLine.objects.get(pk=line.pk).article == "01.5600.000"
        assert not CustomsOrderNumberChange.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(leaves)
