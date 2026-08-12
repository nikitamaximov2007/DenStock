"""Настройки для тестов: SQLite в памяти, быстрый запуск без Docker/PostgreSQL."""
from .base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["*"]
DENSTOCK_MODE = "test"
DENSTOCK_INSTANCE_ID = "test"

_test_database_url = env("DENSTOCK_TEST_DATABASE_URL", default="").strip()  # noqa: F405
if _test_database_url:
    DATABASES = {"default": env.db("DENSTOCK_TEST_DATABASE_URL")}  # noqa: F405
    _emergency_database_name = env(  # noqa: F405
        "DENSTOCK_EMERGENCY_DATABASE_NAME", default=""
    ).strip()
    if _emergency_database_name:
        DATABASES["default"]["NAME"] = _emergency_database_name
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

# Быстрее хеширование паролей в тестах.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
