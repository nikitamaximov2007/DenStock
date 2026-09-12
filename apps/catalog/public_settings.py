"""Settings of the public catalog process that the test suite also applies.

Pure constants: this module is imported while Django settings load, so it
must not import models, views or anything that needs the app registry.
"""

PUBLIC_MIDDLEWARE = [
    "apps.core.observability.RequestIdMiddleware",
    "apps.catalog.public_runtime.PublicAccessLogMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.catalog.public_runtime.PublicResponsePolicyMiddleware",
    "apps.catalog.public_runtime.PublicDatabaseUnavailableMiddleware",
]

PUBLIC_CONTEXT_PROCESSORS = [
    "django.template.context_processors.request",
    "django.contrib.messages.context_processors.messages",
]

# Everything the public process needs besides the database, hosts and secret.
PUBLIC_SETTINGS = {
    "ROOT_URLCONF": "config.public_urls",
    "MIDDLEWARE": PUBLIC_MIDDLEWARE,
    # The cart is the only session content and it lives in the signed cookie,
    # so the public role never needs the django_session table.
    "SESSION_ENGINE": "django.contrib.sessions.backends.signed_cookies",
    "SESSION_COOKIE_NAME": "prostor_cart",
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_AGE": 30 * 24 * 60 * 60,
    "CSRF_COOKIE_NAME": "prostor_csrf",
    "CSRF_COOKIE_HTTPONLY": True,
    "CSRF_COOKIE_SAMESITE": "Lax",
    "CSRF_FAILURE_VIEW": "apps.catalog.public_views.csrf_failure",
    "MESSAGE_STORAGE": "django.contrib.messages.storage.cookie.CookieStorage",
    "DATA_UPLOAD_MAX_MEMORY_SIZE": 64 * 1024,
    "DATA_UPLOAD_MAX_NUMBER_FIELDS": 32,
    "X_FRAME_OPTIONS": "DENY",
    "SECURE_CONTENT_TYPE_NOSNIFF": True,
    "SECURE_REFERRER_POLICY": "same-origin",
}

# Скрипт на публичных страницах ровно один: маска телефона в форме заявки
# (`static/shared/phone_input.js`). Поэтому `script-src 'self'` без `unsafe-inline`
# и `unsafe-eval`: встроенный код, внешние домены, аналитика и реклама
# по-прежнему запрещены, а `default-src 'none'` закрывает всё остальное
# (fetch, websocket, шрифты, медиа, фреймы).
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "img-src 'self'",
        "style-src 'self'",
        "script-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
    )
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=()"
