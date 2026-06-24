"""Настройки для тестов: SQLite в памяти, быстрый запуск без Docker/PostgreSQL."""
from .base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Быстрее хеширование паролей в тестах.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
