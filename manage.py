#!/usr/bin/env python
"""Командная утилита Django для DenisStock."""
import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Не удалось импортировать Django. Активировано ли виртуальное "
            "окружение и установлены ли зависимости?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
