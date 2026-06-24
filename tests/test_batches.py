from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.procurement.models import Batch, BatchLine
from apps.suppliers.models import Supplier

PASSWORD = "parol-12345"


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
def refs(db):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    part = PartType.objects.create(
        name="Насос", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    return {"sup": sup, "part": part}


def _batch_payload(refs, **over):
    data = {
        "supplier": refs["sup"].pk,
        "currency": "RUB",
        "exchange_rate": "1",
        "goods_total": "0",
        "shipping_cost": "0",
        "customs_cost": "0",
        "commission_cost": "0",
        "other_cost": "0",
    }
    data.update(over)
    return data


def _make_batch(refs, **over):
    fields = {"supplier": refs["sup"]}
    fields.update(over)
    return Batch.objects.create(**fields)


def _make_line(batch, part, **over):
    fields = {
        "batch": batch,
        "part_type": part,
        "quantity": Decimal("3"),
        "unit_cost_currency": Decimal("14700.00"),
    }
    fields.update(over)
    return BatchLine.objects.create(**fields)


def test_create_batch(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    resp = client.post(reverse("batch_create"), _batch_payload(refs))
    assert resp.status_code == 302
    batch = Batch.objects.get()
    assert batch.number.startswith("П-")


def test_batch_number_generated_without_dup(refs):
    b1 = _make_batch(refs)
    b2 = _make_batch(refs)
    assert b1.number != b2.number
    assert b1.number.startswith("П-") and b2.number.startswith("П-")


def test_create_line_via_view(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    batch = _make_batch(refs)
    resp = client.post(
        reverse("batch_line_add", args=[batch.pk]),
        {"part_type": refs["part"].pk, "quantity": "2", "unit_cost_currency": "100.00"},
    )
    assert resp.status_code == 302
    assert BatchLine.objects.filter(batch=batch).count() == 1


def test_line_total_uses_decimal(refs):
    batch = _make_batch(refs, exchange_rate=Decimal("1"))
    line = _make_line(
        batch, refs["part"], quantity=Decimal("3"), unit_cost_currency=Decimal("14700.00")
    )
    assert line.total_cost_rub == Decimal("44100.00")
    # дробное количество и копейки считаются точно
    line2 = _make_line(
        batch, refs["part"], quantity=Decimal("2.5"), unit_cost_currency=Decimal("10.10")
    )
    assert line2.total_cost_currency == Decimal("25.25")


def test_batchline_does_not_create_stock(refs):
    from apps.inventory.models import PartItem

    batch = _make_batch(refs)
    _make_line(batch, refs["part"])
    # Строка партии сама по себе не создаёт физических экземпляров/остатка
    # (экземпляры появляются только явным действием на Слое 8).
    assert PartItem.objects.count() == 0


def test_cost_finalized_default_false(refs):
    batch = _make_batch(refs)
    assert batch.cost_finalized is False


def test_batch_not_available_for_sale(refs):
    batch = _make_batch(refs)
    assert batch.is_available_for_sale is False


def test_line_delete_only_in_draft(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    batch = _make_batch(refs, status=Batch.Status.DRAFT)
    line = _make_line(batch, refs["part"])
    resp = client.post(reverse("batch_line_delete", args=[line.pk]))
    assert resp.status_code == 302
    assert not BatchLine.objects.filter(pk=line.pk).exists()


def test_line_delete_blocked_after_draft(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    batch = _make_batch(refs, status=Batch.Status.ORDERED)
    line = _make_line(batch, refs["part"])
    resp = client.post(reverse("batch_line_delete", args=[line.pk]))
    assert resp.status_code == 302
    assert BatchLine.objects.filter(pk=line.pk).exists()  # не удалена


def test_status_transition_valid_and_invalid(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    batch = _make_batch(refs, status=Batch.Status.DRAFT)
    client.post(reverse("batch_status_change", args=[batch.pk]), {"status": Batch.Status.ORDERED})
    batch.refresh_from_db()
    assert batch.status == Batch.Status.ORDERED
    # Недопустимый переход (ordered -> accepted напрямую) игнорируется
    client.post(reverse("batch_status_change", args=[batch.pk]), {"status": Batch.Status.ACCEPTED})
    batch.refresh_from_db()
    assert batch.status == Batch.Status.ORDERED


def test_non_manager_cannot_create_batch(make_user, client, refs):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("batch_create"), _batch_payload(refs))
    assert resp.status_code == 403
    assert not Batch.objects.exists()


def test_costs_hidden_from_storekeeper(make_user, client, refs):
    make_user("admin", is_superuser=True)
    make_user("sklad", role=roles.STOREKEEPER)
    batch = _make_batch(refs)
    _make_line(batch, refs["part"])  # total_cost_rub = 44100.00

    client.login(username="sklad", password=PASSWORD)
    sklad_html = client.get(reverse("batch_detail", args=[batch.pk])).content.decode()
    assert "44100" not in sklad_html  # суммы скрыты

    client.logout()
    client.login(username="admin", password=PASSWORD)
    admin_html = client.get(reverse("batch_detail", args=[batch.pk])).content.decode()
    assert "44100" in admin_html  # админу видны


def test_seller_cannot_view_batches(make_user, client, refs):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("batch_list")).status_code == 403


def test_navigation_batches_visible_to_right_roles(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    make_user("prodavec", role=roles.SELLER)

    client.login(username="sklad", password=PASSWORD)
    assert "Партии" in client.get(reverse("dashboard")).content.decode()

    client.logout()
    client.login(username="prodavec", password=PASSWORD)
    assert "Партии" not in client.get(reverse("dashboard")).content.decode()
