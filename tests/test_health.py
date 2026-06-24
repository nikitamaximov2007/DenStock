import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_healthz_returns_ok(client):
    """Приложение отвечает и видит базу данных."""
    resp = client.get(reverse("healthz"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"


def test_custom_user_model_is_active():
    """Используется кастомная модель пользователя из accounts."""
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    assert user_model.__name__ == "User"
    assert user_model._meta.app_label == "accounts"
