"""Restricted settings for the public catalog runtime.

This process deliberately has no internal URL configuration and no
authentication middleware. It uses a separate, SELECT-only database role in
deployment; migrations are run by the privileged internal release job. The
request stack, cookies and error handlers come from
``apps.catalog.public_settings`` so the tests exercise exactly this policy.
"""

from apps.catalog.public_settings import PUBLIC_CONTEXT_PROCESSORS, PUBLIC_SETTINGS

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
# Reuse a connection for a minute instead of opening one per request: less
# latency and far less connection churn on the shared database. The role's
# CONNECTION LIMIT caps the total; health checks drop a dead connection.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("PUBLIC_DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

globals().update(PUBLIC_SETTINGS)
TEMPLATES[0]["OPTIONS"]["context_processors"] = list(PUBLIC_CONTEXT_PROCESSORS)  # noqa: F405

# --- Search engines ------------------------------------------------------------
# Indexing is OFF unless the deployment explicitly turns it on, so a preview
# or a misconfigured host fails closed to noindex. Launch: set
# PUBLIC_CATALOG_INDEXING=true and PUBLIC_CATALOG_BASE_URL=https://<domain>,
# and remove the edge X-Robots-Tag header for that host.
PUBLIC_CATALOG_INDEXING = env.bool("PUBLIC_CATALOG_INDEXING", default=False)
PUBLIC_CATALOG_BASE_URL = env("PUBLIC_CATALOG_BASE_URL", default="").strip().rstrip("/")

# --- Transport ------------------------------------------------------------------
# Caddy terminates TLS and forwards X-Forwarded-Proto (prod.py trusts it).
# Cookies are Secure unless explicitly turned off for a local HTTP run. prod.py
# reads the same variable without a boolean cast, where "false" is truthy.
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env.bool("DJANGO_SECURE_COOKIES", default=True)
# HSTS stays opt-in per host: a preview hostname must not be pinned by accident.
SECURE_HSTS_SECONDS = env.int("PUBLIC_HSTS_SECONDS", default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

# --- Static assets ----------------------------------------------------------------
# Only the public asset folder is served, straight from the image through the
# staticfiles finder. Internal JS/CSS and the Django admin assets are not part
# of this process at all, and no collectstatic step is needed.
STATICFILES_DIRS = [("public_catalog", BASE_DIR / "static" / "public_catalog")]  # noqa: F405
STATICFILES_FINDERS = ["django.contrib.staticfiles.finders.FileSystemFinder"]
STATIC_ROOT = None
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = False

# The public service has no media URL or media mount. Published catalog photos
# are re-encoded copies read from the database; all uploads stay internal.
MEDIA_URL = ""
MEDIA_ROOT = BASE_DIR / ".public-media-disabled"  # noqa: F405
PRIVATE_MEDIA_ROOT = BASE_DIR / ".public-private-media-disabled"  # noqa: F405

# --- Logs -------------------------------------------------------------------------
# One access line per request (route, status, latency) next to the existing
# error channel. The formatter omits query strings, bodies and cookies.
LOGGING["handlers"]["public_access"] = {  # noqa: F405
    "class": "logging.StreamHandler",
    "stream": "ext://sys.stderr",
    "level": "INFO",
    "formatter": "operational",
    "filters": ["request_context"],
}
LOGGING["loggers"]["apps.catalog.public.access"] = {  # noqa: F405
    "handlers": ["public_access"],
    "level": "INFO",
    "propagate": False,
}

# Fail closed if an internal integration is accidentally configured here.
AI_SUPPORT_ENABLED = False
AI_SUPPORT_PROVIDER = "disabled"
DENSTOCK_ENABLE_WEB_RESTORE = False
DENSTOCK_MANIFEST_SIGNING_KEY_PATH = ""
DENSTOCK_MANIFEST_PUBLIC_KEY_PATH = ""
DENSTOCK_MANIFEST_SIGNING_KEY_ID = ""
