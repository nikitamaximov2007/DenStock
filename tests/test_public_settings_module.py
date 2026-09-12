"""config.settings.public loads fail-closed defaults from its environment."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings

PROBE = """
import json
import config.settings.public as s
print(json.dumps({
    "debug": s.DEBUG,
    "urlconf": s.ROOT_URLCONF,
    "middleware": s.MIDDLEWARE,
    "session_engine": s.SESSION_ENGINE,
    "secure_cookies": [s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE],
    "indexing": s.PUBLIC_CATALOG_INDEXING,
    "base_url": s.PUBLIC_CATALOG_BASE_URL,
    "hsts": s.SECURE_HSTS_SECONDS,
    "conn_max_age": s.DATABASES["default"]["CONN_MAX_AGE"],
    "media_url": s.MEDIA_URL,
    "static_root": s.STATIC_ROOT,
    "finders": s.STATICFILES_FINDERS,
    "static_dirs": [[prefix, str(path)] for prefix, path in s.STATICFILES_DIRS],
    "context_processors": s.TEMPLATES[0]["OPTIONS"]["context_processors"],
    "ai": s.AI_SUPPORT_ENABLED,
    "admin_loaded": "django.contrib.admin" in s.INSTALLED_APPS,
    "restore": s.DENSTOCK_ENABLE_WEB_RESTORE,
}))
"""


def _load(**extra):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DJANGO_", "PUBLIC_", "DATABASE_URL", "DENSTOCK_"))
    }
    env.update(
        {
            "DJANGO_SECRET_KEY": "public-settings-probe",
            "DJANGO_ALLOWED_HOSTS": "catalog.example",
            "DJANGO_PUBLIC_ALLOWED_HOSTS": "catalog.example",
            "PUBLIC_DATABASE_URL": "postgres://denstock_public:x@db:5432/denstock",
            **extra,
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_public_settings_fail_closed_by_default():
    loaded = _load()
    from apps.catalog.public_settings import PUBLIC_CONTEXT_PROCESSORS, PUBLIC_MIDDLEWARE

    assert loaded["debug"] is False
    assert loaded["urlconf"] == "config.public_urls"
    assert loaded["middleware"] == PUBLIC_MIDDLEWARE
    assert loaded["context_processors"] == PUBLIC_CONTEXT_PROCESSORS
    assert loaded["session_engine"] == "django.contrib.sessions.backends.signed_cookies"
    assert loaded["secure_cookies"] == [True, True]
    assert loaded["indexing"] is False
    assert loaded["hsts"] == 0
    assert loaded["conn_max_age"] == 60
    assert loaded["media_url"] == "" and loaded["static_root"] is None
    assert loaded["finders"] == ["django.contrib.staticfiles.finders.FileSystemFinder"]
    assert [prefix for prefix, _ in loaded["static_dirs"]] == ["public_catalog", "shared"]
    assert loaded["ai"] is False and loaded["restore"] is False
    assert loaded["admin_loaded"] is False


def test_public_settings_pass_the_django_system_check():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DJANGO_", "PUBLIC_", "DATABASE_URL", "DENSTOCK_"))
    }
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.public",
            "DJANGO_SECRET_KEY": "public-settings-probe",
            "DJANGO_ALLOWED_HOSTS": "catalog.example",
            "DJANGO_PUBLIC_ALLOWED_HOSTS": "catalog.example",
            "PUBLIC_DATABASE_URL": "postgres://denstock_public:x@db:5432/denstock",
        }
    )
    result = subprocess.run(
        [sys.executable, "manage.py", "check"],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "no issues" in result.stdout


def test_public_settings_launch_switches_are_environment_only():
    loaded = _load(
        PUBLIC_CATALOG_INDEXING="true",
        PUBLIC_CATALOG_BASE_URL="https://pro-stor.ru/",
        PUBLIC_HSTS_SECONDS="31536000",
        DJANGO_SECURE_COOKIES="false",
    )
    assert loaded["indexing"] is True
    assert loaded["base_url"] == "https://pro-stor.ru"
    assert loaded["hsts"] == 31536000
    assert loaded["secure_cookies"] == [False, False]


def test_public_settings_refuse_to_start_without_their_database_or_hosts():
    for missing in ("PUBLIC_DATABASE_URL", "DJANGO_PUBLIC_ALLOWED_HOSTS"):
        env = {key: value for key, value in os.environ.items() if key != missing}
        env.update(
            {
                "DJANGO_SECRET_KEY": "public-settings-probe",
                "DJANGO_ALLOWED_HOSTS": "catalog.example",
                "DJANGO_PUBLIC_ALLOWED_HOSTS": "catalog.example",
                "PUBLIC_DATABASE_URL": "postgres://denstock_public:x@db:5432/denstock",
            }
        )
        env.pop(missing)
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings.public"],
            cwd=settings.BASE_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0 and missing in result.stderr, missing


def test_the_public_runtime_serves_every_asset_its_pages_ask_for():
    """Публичная страница не имеет права ссылаться на файл, которого нет.

    Публичный процесс отдаёт только свою папку и общую: `static/js` на
    публичном хосте не открыт совсем. Поэтому ссылка на внутренний путь была бы
    молчаливым 404 - именно так маска телефона однажды и не загрузилась.
    """
    dirs = {prefix: Path(path) for prefix, path in _load()["static_dirs"]}
    templates = sorted((Path(settings.BASE_DIR) / "templates" / "public_catalog").rglob("*.html"))
    assert templates

    asked = set()
    for template in templates:
        text = template.read_text(encoding="utf-8")
        asked.update(re.findall(r"{%\s*(?:public_asset|static)\s+'([^']+)'", text))
    assert "shared/phone_input.js" in asked, "маска телефона обязана подключаться"

    for path in sorted(asked):
        prefix, _, rest = path.partition("/")
        root = dirs.get(prefix)
        assert root is not None, f"публичный процесс не отдаёт префикс {prefix!r} ({path})"
        assert (root / rest).is_file(), f"нет файла {path}"


def test_the_public_runtime_does_not_expose_internal_scripts():
    dirs = [Path(path) for _, path in _load()["static_dirs"]]
    internal = {"app_shell.js", "ai_support.js", "partial_navigation.js", "scanner.js"}

    served = {item.name for root in dirs for item in root.rglob("*") if item.is_file()}

    assert not (served & internal), served & internal
