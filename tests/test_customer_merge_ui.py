"""Narrow, manager-only internal UI for comparing and merging duplicate cards.

Never auto-merges on page load: customer_detail only shows a banner and a
link to compare; the merge itself always requires an explicit POST from the
confirmation screen.
"""

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.customers.models import Customer, CustomerMergeReceipt

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


def _login_manager(client, make_user, name="manager"):
    make_user(name, role=roles.MANAGER)
    client.login(username=name, password=PASSWORD)


def _login_seller(client, make_user, name="seller"):
    make_user(name, role=roles.SELLER)
    client.login(username=name, password=PASSWORD)


def test_customer_detail_shows_duplicate_banner_for_manager(client, make_user, db):
    first = Customer.objects.create(name="Первый", phone="+7 900 111-22-33")
    Customer.objects.create(name="Второй", phone="8 900 111 22 33")
    _login_manager(client, make_user)

    response = client.get(reverse("customer_detail", args=[first.pk]))

    assert response.status_code == 200
    assert "Обнаружены карточки с тем же телефоном" in response.content.decode()
    assert "Сравнить карточки" in response.content.decode()


def test_customer_detail_hides_merge_link_for_non_manager(client, make_user, db):
    first = Customer.objects.create(name="Первый", phone="+7 900 111-22-33")
    Customer.objects.create(name="Второй", phone="8 900 111 22 33")
    _login_seller(client, make_user)

    response = client.get(reverse("customer_detail", args=[first.pk]))

    assert response.status_code == 200
    assert "Сравнить карточки" not in response.content.decode()


def test_customer_detail_shows_no_banner_without_duplicates(client, make_user, db):
    only = Customer.objects.create(name="Один", phone="+7 900 111-22-33")
    _login_manager(client, make_user)

    response = client.get(reverse("customer_detail", args=[only.pk]))

    assert "Обнаружены карточки с тем же телефоном" not in response.content.decode()


def test_compare_view_requires_manager_role(client, make_user, db):
    first = Customer.objects.create(name="Первый", phone="+7 900 111-22-33")
    second = Customer.objects.create(name="Второй", phone="8 900 111 22 33")
    _login_seller(client, make_user)

    response = client.get(reverse("customer_compare", args=[first.pk, second.pk]))

    assert response.status_code == 403


def test_compare_view_shows_both_cards_for_manager(client, make_user, db):
    first = Customer.objects.create(name="Первый Иванов", phone="+7 900 111-22-33")
    second = Customer.objects.create(name="Второй Петров", phone="8 900 111 22 33")
    _login_manager(client, make_user)

    response = client.get(reverse("customer_compare", args=[first.pk, second.pk]))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Первый Иванов" in body
    assert "Второй Петров" in body


def test_merge_confirm_shows_the_move_plan(client, make_user, db):
    from apps.sales.models import Sale

    first = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    second = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    Sale.objects.create(
        status=Sale.Status.COMPLETED, customer=second,
        customer_name=second.name, customer_phone=second.phone,
    )
    _login_manager(client, make_user)

    response = client.get(
        reverse("customer_merge_confirm", args=[first.pk, second.pk]) + f"?target={first.pk}"
    )

    assert response.status_code == 200
    assert "sales: 1" in response.content.decode()


def test_merge_apply_requires_manager_role(client, make_user, db):
    first = Customer.objects.create(name="Первый", phone="+7 900 111-22-33")
    second = Customer.objects.create(name="Второй", phone="8 900 111 22 33")
    _login_seller(client, make_user)

    response = client.post(
        reverse("customer_merge_apply"), {"target_id": first.pk, "source_id": second.pk}
    )

    assert response.status_code == 403
    assert not Customer.objects.get(pk=second.pk).is_merged


def test_merge_apply_merges_and_redirects_to_target(client, make_user, db):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    _login_manager(client, make_user)

    response = client.post(
        reverse("customer_merge_apply"),
        {"target_id": target.pk, "source_id": source.pk, "reason": "тот же клиент"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("customer_detail", args=[target.pk])
    source.refresh_from_db()
    assert source.merged_into_id == target.pk
    receipt = CustomerMergeReceipt.objects.get(source=source, target=target)
    assert receipt.reason == "тот же клиент"


def test_merge_apply_never_runs_on_a_bare_page_load(client, make_user, db):
    """GET never merges - only the explicit POST from the confirm screen does."""
    Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    _login_manager(client, make_user)

    response = client.get(reverse("customer_merge_apply"))

    assert response.status_code == 405
    source.refresh_from_db()
    assert source.merged_into_id is None


def test_merge_apply_rejects_self_merge_with_error_message(client, make_user, db):
    customer = Customer.objects.create(name="Один", phone="+7 900 111-22-33")
    _login_manager(client, make_user)

    response = client.post(
        reverse("customer_merge_apply"), {"target_id": customer.pk, "source_id": customer.pk}
    )

    assert response.status_code == 302
    customer.refresh_from_db()
    assert customer.merged_into_id is None
