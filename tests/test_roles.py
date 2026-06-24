import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_active=True, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(
                username=username, password=PASSWORD, is_active=is_active
            )
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


def test_role_groups_created_by_migration(db):
    names = set(Group.objects.values_list("name", flat=True))
    assert set(roles.ALL_ROLES) <= names


def test_superuser_has_admin_capabilities(make_user):
    admin = make_user("super", is_superuser=True)
    assert admin.can_manage_users is True
    assert admin.capabilities == set(roles.ALL_CAPABILITIES)
    # Сигнал добавил суперпользователя в группу «Администратор».
    assert roles.ADMIN in admin.role_names


def test_combined_roles_sum_capabilities(make_user):
    user = make_user("multi", role=roles.STOREKEEPER)
    user.groups.add(Group.objects.get(name=roles.MANAGER))
    assert user.can_edit is True  # от кладовщика
    assert user.can_view_finance is True  # от руководителя
    assert user.can_confirm_adjustments is True


def test_admin_sees_user_management(make_user, client):
    make_user("super", is_superuser=True)
    client.login(username="super", password=PASSWORD)
    resp = client.get(reverse("user_list"))
    assert resp.status_code == 200
    assert "Пользователи" in resp.content.decode()


def test_non_admin_nav_has_no_user_management(make_user, client):
    make_user("seller", role=roles.SELLER)
    client.login(username="seller", password=PASSWORD)
    html = client.get(reverse("dashboard")).content.decode()
    assert "Пользователи" not in html


def test_non_admin_gets_403_on_user_list(make_user, client):
    make_user("seller", role=roles.SELLER)
    client.login(username="seller", password=PASSWORD)
    resp = client.get(reverse("user_list"))
    assert resp.status_code == 403


def test_role_persists_on_edit(make_user, client):
    make_user("super", is_superuser=True)
    target = make_user("vasya")
    client.login(username="super", password=PASSWORD)
    storekeeper = Group.objects.get(name=roles.STOREKEEPER)
    resp = client.post(
        reverse("user_edit", args=[target.pk]),
        {"username": "vasya", "full_name": "Вася", "is_active": "on", "groups": [storekeeper.pk]},
    )
    assert resp.status_code == 302
    target.refresh_from_db()
    assert roles.STOREKEEPER in target.role_names


def test_inactive_user_cannot_login(make_user, client):
    make_user("ghost", is_active=False)
    assert client.login(username="ghost", password=PASSWORD) is False


def test_navigation_changes_by_role(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("dashboard")).content.decode()
    assert "Статистика" not in html  # кладовщик не видит финансы
    assert "Пользователи" not in html
    assert "Поступление" in html  # но видит склад

    client.logout()
    make_user("ruk", role=roles.MANAGER)
    client.login(username="ruk", password=PASSWORD)
    html2 = client.get(reverse("dashboard")).content.decode()
    assert "Статистика" in html2  # руководитель видит финансы


def test_viewer_cannot_edit(make_user):
    viewer = make_user("nabl", role=roles.VIEWER)
    assert viewer.can_edit is False
    assert viewer.can_view_finance is True


def test_admin_cannot_deactivate_self(make_user, client):
    admin = make_user("super", is_superuser=True)
    client.login(username="super", password=PASSWORD)
    resp = client.post(reverse("user_toggle", args=[admin.pk]))
    assert resp.status_code == 302
    admin.refresh_from_db()
    assert admin.is_active is True
