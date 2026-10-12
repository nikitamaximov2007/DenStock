"""WSGI-точка входа DenisStock."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()

# Offline DR: never serve a database or file tree of an unfinished cutover.
from apps.operations.cutover_guard import assert_startup_allowed  # noqa: E402

assert_startup_allowed()
