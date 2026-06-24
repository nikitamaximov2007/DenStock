"""Настройки для боевого запуска (склад / VPS)."""
from .base import *  # noqa: F403
from .base import env

DEBUG = False

# В проде секреты обязаны приходить из окружения.
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# Базовые меры безопасности за reverse-proxy (Caddy).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = env("DJANGO_SECURE_COOKIES", default=False)
CSRF_COOKIE_SECURE = env("DJANGO_SECURE_COOKIES", default=False)
CSRF_TRUSTED_ORIGINS = env("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
