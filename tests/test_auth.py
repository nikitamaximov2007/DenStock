import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_dashboard_requires_login(client):
    resp = client.get(reverse("dashboard"))
    assert resp.status_code == 302
    assert reverse("login") in resp["Location"]


@pytest.mark.django_db
def test_login_then_dashboard_accessible(client, django_user_model):
    django_user_model.objects.create_user(username="kladovshik", password="parol-12345")
    resp = client.post(
        reverse("login"),
        {"username": "kladovshik", "password": "parol-12345"},
    )
    assert resp.status_code == 302
    assert resp["Location"] == reverse("dashboard")

    resp_dashboard = client.get(reverse("dashboard"))
    assert resp_dashboard.status_code == 200


@pytest.mark.django_db
def test_logout(client, django_user_model):
    django_user_model.objects.create_user(username="prodavec", password="parol-12345")
    client.login(username="prodavec", password="parol-12345")
    resp = client.post(reverse("logout"))
    assert resp.status_code == 302
