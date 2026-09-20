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
    "apps.catalog_import",
    "apps.polaris",
    "apps.counting",
    "apps.actions",
    "apps.customers",
    "apps.sales",
    "apps.repairs",
    "apps.ordered_parts",
    "apps.customer_requests",
    "apps.customer_accounts",
    "apps.customs_orders",
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
    # Первым: номер запроса нужен всему, что может упасть ниже по цепочке.
    "apps.core.observability.RequestIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.operations.write_guard.BusinessWriteGuardMiddleware",
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
                "apps.operations.context_processors.emergency_mode",
            ],
            # Русский формат дат в UI доступен во всех шаблонах без {% load %}.
            "builtins": [
                "apps.core.templatetags.ru_dates",
                "apps.core.templatetags.number_format",
                "apps.core.templatetags.storage_address",
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
_emergency_database_name = env("DENSTOCK_EMERGENCY_DATABASE_NAME", default="").strip()
if _emergency_database_name:
    if env("DENSTOCK_MODE", default="development").strip().lower() != "emergency-local":
        raise ValueError(
            "DENSTOCK_EMERGENCY_DATABASE_NAME is allowed only in emergency-local mode."
        )
    DATABASES["default"]["NAME"] = _emergency_database_name

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
MEDIA_ROOT = Path(env("DENSTOCK_MEDIA_ROOT", default=str(BASE_DIR / "mediafiles")))

# Private AI support screenshots are served only through authenticated Django
# views. This directory must never be mounted into the public Caddy media path.
PRIVATE_MEDIA_ROOT = Path(
    env("PRIVATE_MEDIA_ROOT", default=str(BASE_DIR / "private_media"))
)

# --- Read-only AI support ---------------------------------------------------
AI_SUPPORT_ENABLED = env.bool("AI_SUPPORT_ENABLED", default=False)
AI_SUPPORT_PROVIDER = env("AI_SUPPORT_PROVIDER", default="disabled")
AI_SUPPORT_ALLOW_FAKE_PROVIDER = False
AI_SUPPORT_CODEX_BINARY = env("AI_SUPPORT_CODEX_BINARY", default="codex")
AI_SUPPORT_CODEX_REQUIRED_VERSION = env(
    "AI_SUPPORT_CODEX_REQUIRED_VERSION", default="0.142.5"
)
AI_SUPPORT_CODEX_MODEL = env("AI_SUPPORT_CODEX_MODEL", default="")
AI_SUPPORT_CODEX_HOME = env("AI_SUPPORT_CODEX_HOME", default="")
AI_SUPPORT_CODEX_WORKSPACE = env("AI_SUPPORT_CODEX_WORKSPACE", default="")
AI_SUPPORT_CODEX_LAUNCH_MODE = env("AI_SUPPORT_CODEX_LAUNCH_MODE", default="disabled")
AI_SUPPORT_CODEX_LAUNCHER_SOCKET = env(
    "AI_SUPPORT_CODEX_LAUNCHER_SOCKET",
    default="/run/denstock-ai/launcher.sock",
)
AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION = env.bool(
    "AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION", default=False
)
AI_SUPPORT_CODEX_TIMEOUT_SECONDS = env.int("AI_SUPPORT_CODEX_TIMEOUT_SECONDS", default=60)
AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES = env.int(
    "AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES", default=64 * 1024
)
AI_SUPPORT_CODEX_MAX_STDERR_BYTES = env.int(
    "AI_SUPPORT_CODEX_MAX_STDERR_BYTES", default=16 * 1024
)
AI_SUPPORT_CODEX_MAX_PROMPT_CHARS = env.int(
    "AI_SUPPORT_CODEX_MAX_PROMPT_CHARS", default=24000
)
AI_SUPPORT_CODEX_MAX_HISTORY_CHARS = env.int(
    "AI_SUPPORT_CODEX_MAX_HISTORY_CHARS", default=12000
)
AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = env.int(
    "AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY", default=1
)
AI_SUPPORT_CODEX_RUNTIME_RETENTION_HOURS = env.int(
    "AI_SUPPORT_CODEX_RUNTIME_RETENTION_HOURS", default=24
)
AI_SUPPORT_MAX_MESSAGE_CHARS = env.int("AI_SUPPORT_MAX_MESSAGE_CHARS", default=8000)
AI_SUPPORT_RATE_LIMIT = env.int("AI_SUPPORT_RATE_LIMIT", default=5)
AI_SUPPORT_DAILY_REQUEST_LIMIT = env.int("AI_SUPPORT_DAILY_REQUEST_LIMIT", default=50)
AI_SUPPORT_DAILY_TOKEN_LIMIT = env.int("AI_SUPPORT_DAILY_TOKEN_LIMIT", default=100000)
AI_SUPPORT_MAX_IMAGE_BYTES = env.int("AI_SUPPORT_MAX_IMAGE_BYTES", default=5 * 1024 * 1024)

# Draft identifiers for public-request acceptance evidence. They deliberately
# carry no legal wording: production values/text must be approved before launch.
PUBLIC_REQUEST_PRIVACY_POLICY_VERSION = env(
    "PUBLIC_REQUEST_PRIVACY_POLICY_VERSION", default="draft-legal-review-1"
).strip()
PUBLIC_REQUEST_PERSONAL_DATA_CONSENT_VERSION = env(
    "PUBLIC_REQUEST_PERSONAL_DATA_CONSENT_VERSION", default="draft-legal-review-1"
).strip()
# New public requests per client address and catalog-web process in a window.
PUBLIC_REQUEST_RATE_LIMIT = env.int("PUBLIC_REQUEST_RATE_LIMIT", default=5)
PUBLIC_REQUEST_RATE_WINDOW_SECONDS = env.int("PUBLIC_REQUEST_RATE_WINDOW_SECONDS", default=600)
TELEGRAM_BOT_USERNAME = env("TELEGRAM_BOT_USERNAME", default="").strip().lstrip("@")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", default="").strip()
TELEGRAM_REQUEST_LINK_TTL_SECONDS = env.int("TELEGRAM_REQUEST_LINK_TTL_SECONDS", default=86400)
# Where the customer's deep link points. Production keeps Telegram's own domain;
# a local origin is allowed so browser tests never touch the real t.me. The
# public success page also derives its CSP form-action source from this value:
# Chromium applies form-action to the whole redirect chain of a form POST.
TELEGRAM_DEEP_LINK_BASE_URL = env("TELEGRAM_DEEP_LINK_BASE_URL", default="https://t.me").strip()
# Telegram request bot (long polling service `telegram-bot`). The token is a
# secret: it is read only from the environment of that service and never
# stored, rendered or logged. Empty means "not configured".
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="").strip()
TELEGRAM_API_BASE_URL = env(
    "TELEGRAM_API_BASE_URL", default="https://api.telegram.org"
).strip().rstrip("/")
TELEGRAM_POLL_TIMEOUT_SECONDS = env.int("TELEGRAM_POLL_TIMEOUT_SECONDS", default=10)
# Explicit HTTP CONNECT proxy for Bot API calls of the telegram-bot service only
# (docker-compose.yml sets it for that service). Empty means a direct connection.
TELEGRAM_API_PROXY_URL = env("TELEGRAM_API_PROXY_URL", default="").strip()
# Absolute internal DenisStock address for the operators' «Открыть заявку» button.
TELEGRAM_INTERNAL_BASE_URL = env("TELEGRAM_INTERNAL_BASE_URL", default="").strip().rstrip("/")
TELEGRAM_BOT_HEARTBEAT_FILE = env(
    "TELEGRAM_BOT_HEARTBEAT_FILE", default="/tmp/denstock-telegram-bot.heartbeat"
).strip()
MAX_REQUEST_LINK_TTL_SECONDS = env.int("MAX_REQUEST_LINK_TTL_SECONDS", default=86400)
# MAX request bot. Three processes, three different needs:
# * catalog-web builds the deep link: MAX_BOT_USERNAME and MAX_DEEP_LINK_BASE_URL
#   only, never a secret;
# * web receives the webhook: MAX_WEBHOOK_ENABLED and MAX_WEBHOOK_SECRET, never
#   the bot token (receiving needs no API call);
# * max-bot sends: MAX_BOT_TOKEN (from its own env file), never the webhook
#   secret except for the explicit `max_webhook subscribe` command.
# Every default is "off": no token, no secret, webhook disabled.
MAX_BOT_USERNAME = env("MAX_BOT_USERNAME", default="").strip().lstrip("@")
MAX_DEEP_LINK_BASE_URL = env("MAX_DEEP_LINK_BASE_URL", default="https://max.ru").strip()
MAX_BOT_TOKEN = env("MAX_BOT_TOKEN", default="").strip()
MAX_API_BASE_URL = env(
    "MAX_API_BASE_URL", default="https://platform-api2.max.ru"
).strip().rstrip("/")
MAX_API_TIMEOUT_SECONDS = env.int("MAX_API_TIMEOUT_SECONDS", default=15)
# MAX's certificate chains to the Russian Trusted Root CA (Минцифры), absent
# from standard trust stores. The MAX client alone trusts this PEM file, and
# its SHA-256 pins it; verification is never disabled. Empty means the system
# trust store, which production MAX does not pass.
MAX_API_CA_FILE = env("MAX_API_CA_FILE", default="").strip()
MAX_API_CA_SHA256 = env("MAX_API_CA_SHA256", default="").strip()
MAX_WEBHOOK_ENABLED = env.bool("MAX_WEBHOOK_ENABLED", default=False)
MAX_WEBHOOK_SECRET = env("MAX_WEBHOOK_SECRET", default="").strip()
# The public HTTPS address MAX delivers to (port 443, trusted certificate). Used
# only by `manage.py max_webhook`; nothing registers a subscription on its own.
MAX_PUBLIC_WEBHOOK_URL = env("MAX_PUBLIC_WEBHOOK_URL", default="").strip()
MAX_BOT_HEARTBEAT_FILE = env(
    "MAX_BOT_HEARTBEAT_FILE", default="/tmp/denstock-max-bot.heartbeat"
).strip()
AI_SUPPORT_ATTACHMENT_RETENTION_DAYS = env.int(
    "AI_SUPPORT_ATTACHMENT_RETENTION_DAYS", default=30
)
AI_SUPPORT_CONVERSATION_RETENTION_DAYS = env.int(
    "AI_SUPPORT_CONVERSATION_RETENTION_DAYS", default=180
)
DENSTOCK_PUBLIC_BASE_URL = env("DENSTOCK_PUBLIC_BASE_URL", default="")
DENSTOCK_APP_COMMIT = env("DENSTOCK_APP_COMMIT", default="")

# --- Deployment identity and controlled failover ----------------------------
DENSTOCK_MODE = env("DENSTOCK_MODE", default="development").strip().lower()
DENSTOCK_INSTANCE_ID = env("DENSTOCK_INSTANCE_ID", default="development").strip()
DENSTOCK_EMERGENCY_ROOT = Path(
    env("DENSTOCK_EMERGENCY_ROOT", default=str(BASE_DIR / ".emergency"))
)
DENSTOCK_EMERGENCY_DB_PREFIX = env(
    "DENSTOCK_EMERGENCY_DB_PREFIX", default="denstock_emergency_"
)
DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS = env.list(
    "DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS",
    default=["localhost", "127.0.0.1", "::1", "emergency-db"],
)
DENSTOCK_PRODUCTION_DB_HOSTS = env.list(
    "DENSTOCK_PRODUCTION_DB_HOSTS", default=["185.250.44.206", "db"]
)
DENSTOCK_PUBLIC_DATABASE_HOSTS = env.list(
    "DENSTOCK_PUBLIC_DATABASE_HOSTS", default=["db"]
)
DENSTOCK_EMERGENCY_STALE_WARNING_HOURS = env.int(
    "DENSTOCK_EMERGENCY_STALE_WARNING_HOURS", default=24
)
DENSTOCK_EMERGENCY_KEEP_STANDBY = env.int(
    "DENSTOCK_EMERGENCY_KEEP_STANDBY", default=2
)
DENSTOCK_EMERGENCY_KEEP_COMPLETED_EXPORTS = env.int(
    "DENSTOCK_EMERGENCY_KEEP_COMPLETED_EXPORTS", default=2
)
DENSTOCK_EMERGENCY_ROLE = env(
    "DENSTOCK_EMERGENCY_ROLE", default="primary"
).strip().lower()
DENSTOCK_EMERGENCY_WORKSTATION_ID = env("DENSTOCK_EMERGENCY_WORKSTATION_ID", default="").strip()
DENSTOCK_EMERGENCY_WORKSTATION_ID_PATH = env(
    "DENSTOCK_EMERGENCY_WORKSTATION_ID_PATH", default=""
).strip()
DENSTOCK_MANIFEST_SIGNING_KEY_PATH = env("DENSTOCK_MANIFEST_SIGNING_KEY_PATH", default="").strip()
DENSTOCK_MANIFEST_PUBLIC_KEY_PATH = env("DENSTOCK_MANIFEST_PUBLIC_KEY_PATH", default="").strip()
DENSTOCK_MANIFEST_SIGNING_KEY_ID = env("DENSTOCK_MANIFEST_SIGNING_KEY_ID", default="").strip()
DENSTOCK_EMERGENCY_PROBE_TOKEN = env(
    "DENSTOCK_EMERGENCY_PROBE_TOKEN", default=""
)
DENSTOCK_PRODUCTION_URL = env("DENSTOCK_PRODUCTION_URL", default="").strip()
DENSTOCK_BACKUP_STORAGE_ORIGIN = env(
    "DENSTOCK_BACKUP_STORAGE_ORIGIN", default="local"
)

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

# --- Диагностика ошибок ------------------------------------------------------
# Django по умолчанию отдаёт traceback только на почту администраторам, а
# консольный обработчик выключает при DEBUG=False. В бою ни того, ни другого
# нет, и падение у оператора не оставляло следа. Пишем в поток ошибок
# контейнера: его подбирает Docker, а рядом в docker-compose.yml задана
# ротация, чтобы журнал не съел диск.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_context": {"()": "apps.core.observability.RequestContextFilter"},
    },
    "formatters": {
        "operational": {
            "()": "apps.core.observability.RedactingFormatter",
            "format": (
                "%(asctime)s %(levelname)s %(name)s "
                "request=%(request_id)s %(method)s %(path)s user=%(user)s "
                "pid=%(process)d thread=%(threadName)s %(message)s"
            ),
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        },
    },
    "handlers": {
        "operational": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "level": "ERROR",
            "formatter": "operational",
            "filters": ["request_context"],
        },
    },
    "loggers": {
        # propagate=False, иначе та же ошибка уйдёт ещё и в обработчики Django
        # по умолчанию и удвоится в журнале.
        "django.request": {
            "handlers": ["operational"], "level": "ERROR", "propagate": False
        },
        "django.server": {
            "handlers": ["operational"], "level": "ERROR", "propagate": False
        },
        "django.security": {
            "handlers": ["operational"], "level": "ERROR", "propagate": False
        },
        "apps": {"handlers": ["operational"], "level": "ERROR", "propagate": False},
    },
}


# --- PRO-STOR customer account (docs/customer_account/legal_auth_gate.md) ------------------
# Every switch defaults to OFF. The code is complete behind them; turning any of
# them on in production needs the owner's decision and the legal review listed
# in that document. CUSTOMER_ACCOUNT_ENABLED gates the whole feature (pages,
# account creation at messenger handoff, request ownership). The auth switches
# gate individual ways in.
CUSTOMER_ACCOUNT_ENABLED = env.bool("CUSTOMER_ACCOUNT_ENABLED", default=False)
CUSTOMER_MESSENGER_CABINET_ENABLED = env.bool(
    "CUSTOMER_MESSENGER_CABINET_ENABLED", default=False
)
# MAX is the ONLY way to sign in and the only way an account is created (owner
# decision for V1): a Russian-owned system, the candidate under 149-FZ art. 8
# part 10. There is deliberately no Telegram, e-mail, password or SMS login.
# Telegram stays a messaging channel that a MAX-signed-in customer may link.
CUSTOMER_AUTH_MAX_ENABLED = env.bool("CUSTOMER_AUTH_MAX_ENABLED", default=False)
# A login or link attempt lives this long, and the one-time code may be tried
# this many times before the attempt is locked.
CUSTOMER_LOGIN_ATTEMPT_SECONDS = env.int("CUSTOMER_LOGIN_ATTEMPT_SECONDS", default=600)
CUSTOMER_LOGIN_CODE_TRIES = env.int("CUSTOMER_LOGIN_CODE_TRIES", default=5)
CUSTOMER_LOGIN_RATE_LIMIT = env.int("CUSTOMER_LOGIN_RATE_LIMIT", default=10)
CUSTOMER_LOGIN_RATE_WINDOW_SECONDS = env.int("CUSTOMER_LOGIN_RATE_WINDOW_SECONDS", default=900)
CUSTOMER_SESSION_DAYS = env.int("CUSTOMER_SESSION_DAYS", default=30)
# Versions of the texts a customer agrees to in the account. Evidence stores
# the version; the wording itself is approved and published separately.
CUSTOMER_ACCOUNT_CONSENT_VERSION = env("CUSTOMER_ACCOUNT_CONSENT_VERSION", default="").strip()
# Repeat-purchase requests are a separate activation gate.  An empty value
# keeps the technically complete cabinet dormant until the approved wording
# has an explicit version.
CUSTOMER_MESSENGER_REPEAT_CONSENT_VERSION = env(
    "CUSTOMER_MESSENGER_REPEAT_CONSENT_VERSION", default=""
).strip()
# Separate product gate for staff mobile request handling. It stays OFF until
# staff identities are paired and the owner approves activation.
CUSTOMER_OPERATOR_CONSOLE_ENABLED = env.bool(
    "CUSTOMER_OPERATOR_CONSOLE_ENABLED", default=False
)
