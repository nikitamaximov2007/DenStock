"""Restricted settings for the public catalog runtime.

This process deliberately has no internal URL configuration or authentication
middleware. It uses a separate role that can read catalog projections and
write only the minimum request-domain records; migrations are run by the
privileged internal release job.
"""

from .base import env
from .prod import *  # noqa: F403

DENSTOCK_MODE = "public-catalog"
DENSTOCK_INSTANCE_ID = env("DENSTOCK_INSTANCE_ID", default="public-catalog").strip()
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_PUBLIC_ALLOWED_HOSTS", default=[])
if not ALLOWED_HOSTS:
    raise ValueError("DJANGO_PUBLIC_ALLOWED_HOSTS must be set for the public runtime.")

_public_database_url = env("PUBLIC_DATABASE_URL", default="").strip()
if not _public_database_url:
    raise ValueError("PUBLIC_DATABASE_URL must be set for the public runtime.")
DATABASES = {"default": env.db_url_config(_public_database_url)}

ROOT_URLCONF = "config.public_urls"
MIDDLEWARE = [
    "apps.core.observability.RequestIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 32
TEMPLATES[0]["OPTIONS"]["context_processors"] = [  # noqa: F405
    "django.template.context_processors.request",
]

# The public service has no media URL or media mount.  It exposes only static
# assets baked into its image; all user uploads remain internal-only.
MEDIA_URL = ""
MEDIA_ROOT = BASE_DIR / ".public-media-disabled"  # noqa: F405
PRIVATE_MEDIA_ROOT = BASE_DIR / ".public-private-media-disabled"  # noqa: F405

# Fail closed if an internal integration is accidentally configured here.
AI_SUPPORT_ENABLED = False
AI_SUPPORT_PROVIDER = "disabled"
DENSTOCK_ENABLE_WEB_RESTORE = False
DENSTOCK_MANIFEST_SIGNING_KEY_PATH = ""
DENSTOCK_MANIFEST_PUBLIC_KEY_PATH = ""
DENSTOCK_MANIFEST_SIGNING_KEY_ID = ""
