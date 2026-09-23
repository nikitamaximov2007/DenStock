"""Explicit presentation timezones used by operator-facing workflows."""

from zoneinfo import ZoneInfo

from django.utils import timezone

PERM_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
PERM_DATETIME_FORMAT = "%d.%m.%Y %H:%M:%S"


def format_perm_datetime(value) -> str:
    """Format an aware instant as Perm local date and time.

    Stored datetimes remain the authoritative instants. This helper is only a
    presentation conversion and never relies on the process or Django default
    timezone.
    """
    return timezone.localtime(value, PERM_TIMEZONE).strftime(PERM_DATETIME_FORMAT)
