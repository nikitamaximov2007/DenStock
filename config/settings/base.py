"""Базовые настройки DenisStock. Всё чувствительное — через переменные окружения."""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["*"]),
)

# Подхватываем .env, если он есть рядом (локально). В Docker переменные приходят из окружения.
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    env.read_env(_env_file)

# --- Безопасность -----------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-key-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# --- Приложения -------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

# Приложения проекта добавляются по вертикальным слоям (см. docs/design/05-roadmap.md).
LOCAL_APPS = [
    "apps.accounts",
    "apps.core",
    "apps.catalog",
    "apps.suppliers",
    "apps.warehouse",
    "apps.procurement",
    "apps.inventory",
    "apps.receipts",
    "apps.brp",
    "apps.polaris",
    "apps.counting",
    "apps.actions",
    "apps.sales",
    "apps.repairs",
    "apps.returns",
    "apps.writeoffs",
    "apps.stocktaking",
    "apps.reports",
    "apps.labels",
    "apps.operations",
    "apps.ai_support",
]

INSTALLED_APPS = DJANGO_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.navigation",
            ],
            # Русский формат дат в UI доступен во всех шаблонах без {% load %}.
            "builtins": [
                "apps.core.templatetags.ru_dates",
                "apps.core.templatetags.number_format",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- База данных ------------------------------------------------------------
# По умолчанию SQLite (для локального запуска и тестов без Docker).
# В Docker/проде DATABASE_URL указывает на PostgreSQL.
DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

# --- Пользователь -----------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Локализация ------------------------------------------------------------
LANGUAGE_CODE = env("LANGUAGE_CODE", default="ru-ru")
TIME_ZONE = env("TIME_ZONE", default="Europe/Moscow")
USE_I18N = True
USE_TZ = True

# --- Статика и медиа --------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "mediafiles"

# Private AI support screenshots are served only through authenticated Django
# views. This directory must never be mounted into the public Caddy media path.
PRIVATE_MEDIA_ROOT = Path(
    env("PRIVATE_MEDIA_ROOT", default=str(BASE_DIR / "private_media"))
)

# --- Read-only AI support ---------------------------------------------------
AI_SUPPORT_ENABLED = env.bool("AI_SUPPORT_ENABLED", default=False)
AI_SUPPORT_PROVIDER = env("AI_SUPPORT_PROVIDER", default="disabled")
AI_SUPPORT_ALLOW_FAKE_PROVIDER = False
AI_SUPPORT_MODEL = env("AI_SUPPORT_MODEL", default="")
AI_SUPPORT_API_KEY = env("AI_SUPPORT_API_KEY", default="")
AI_SUPPORT_TIMEOUT_SECONDS = env.int("AI_SUPPORT_TIMEOUT_SECONDS", default=20)
AI_SUPPORT_MAX_MESSAGE_CHARS = env.int("AI_SUPPORT_MAX_MESSAGE_CHARS", default=8000)
AI_SUPPORT_MAX_OUTPUT_TOKENS = env.int("AI_SUPPORT_MAX_OUTPUT_TOKENS", default=1200)
AI_SUPPORT_RATE_LIMIT = env.int("AI_SUPPORT_RATE_LIMIT", default=5)
AI_SUPPORT_DAILY_REQUEST_LIMIT = env.int("AI_SUPPORT_DAILY_REQUEST_LIMIT", default=50)
AI_SUPPORT_DAILY_TOKEN_LIMIT = env.int("AI_SUPPORT_DAILY_TOKEN_LIMIT", default=100000)
AI_SUPPORT_MAX_IMAGE_BYTES = env.int("AI_SUPPORT_MAX_IMAGE_BYTES", default=5 * 1024 * 1024)
AI_SUPPORT_ATTACHMENT_RETENTION_DAYS = env.int(
    "AI_SUPPORT_ATTACHMENT_RETENTION_DAYS", default=30
)
AI_SUPPORT_CONVERSATION_RETENTION_DAYS = env.int(
    "AI_SUPPORT_CONVERSATION_RETENTION_DAYS", default=180
)
DENSTOCK_PUBLIC_BASE_URL = env("DENSTOCK_PUBLIC_BASE_URL", default="")
DENSTOCK_APP_COMMIT = env("DENSTOCK_APP_COMMIT", default="")

# --- Эксплуатация (Слой 25) -------------------------------------------------
# Каталог резервных копий (БД + media). Не коммитится (см. .gitignore).
BACKUP_ROOT = Path(env("BACKUP_ROOT", default=str(BASE_DIR / "backups")))

# --- Layer 30: аварийное веб-восстановление ----------------------------------
# По умолчанию ВЫКЛЮЧЕНО. Даже при включённом флаге restore видит только
# администратор из allowlist (email или username). Никаких секретов в Git:
# значения задаются через .env на сервере.
DENSTOCK_ENABLE_WEB_RESTORE = env.bool("DENSTOCK_ENABLE_WEB_RESTORE", default=False)
DENSTOCK_RESTORE_ALLOWED_EMAILS = env.list(
    "DENSTOCK_RESTORE_ALLOWED_EMAILS", default=["nikita.maximov2007@gmail.com"]
)
DENSTOCK_RESTORE_ALLOWED_USERNAMES = env.list(
    "DENSTOCK_RESTORE_ALLOWED_USERNAMES", default=[]
)

# --- Аутентификация (маршруты) ---------------------------------------------
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
