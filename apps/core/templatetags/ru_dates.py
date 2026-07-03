"""Русский формат дат/времени в UI (только отображение).

НЕ меняет хранение в БД, manifest.json, имена папок бэкапов и machine-readable
значения. Фильтры принимают datetime/date, ISO-строку или строку run_id вида
``YYYY-MM-DD_HH-MM-SS`` и возвращают:

- ``ru_date``  -> ``03.07.2026``
- ``ru_dt``    -> ``03.07.2026 05:21``
- ``ru_dts``   -> ``03.07.2026 05:21:19``

Если значение пустое — возвращается пустая строка; если распарсить не удалось —
исходное значение (без падения шаблона).
"""
from datetime import date, datetime

from django import template
from django.utils import timezone

register = template.Library()


def _to_datetime(value):
    """datetime | date | ISO-строка | run_id -> datetime, иначе None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        text = value.strip()
        # run_id бэкапа: 2026-07-03_05-21-19
        try:
            return datetime.strptime(text, "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            pass
        # ISO 8601 (в т.ч. с 'Z' и микросекундами)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _format(value, pattern):
    dt = _to_datetime(value)
    if dt is None:
        # Пусто -> пусто; нераспознанное -> как есть (не роняем шаблон).
        return "" if value in (None, "") else value
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.strftime(pattern)


@register.filter
def ru_date(value):
    """Дата: 03.07.2026."""
    return _format(value, "%d.%m.%Y")


@register.filter
def ru_dt(value):
    """Дата и время до минут: 03.07.2026 05:21."""
    return _format(value, "%d.%m.%Y %H:%M")


@register.filter
def ru_dts(value):
    """Дата и время с секундами: 03.07.2026 05:21:19."""
    return _format(value, "%d.%m.%Y %H:%M:%S")
