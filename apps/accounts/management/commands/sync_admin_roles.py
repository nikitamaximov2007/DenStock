"""Гарантирует, что роли существуют и все суперпользователи в группе «Администратор».

Подстраховка к сигналу: можно запустить вручную для существующих суперпользователей.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from apps.accounts import roles


class Command(BaseCommand):
    help = "Создаёт группы ролей и добавляет суперпользователей в «Администратор»."

    def handle(self, *args, **options):
        for name in roles.ALL_ROLES:
            Group.objects.get_or_create(name=name)

        admin_group = Group.objects.get(name=roles.ADMIN)
        user_model = get_user_model()
        added = 0
        for user in user_model.objects.filter(is_superuser=True):
            if not user.groups.filter(pk=admin_group.pk).exists():
                user.groups.add(admin_group)
                added += 1

        self.stdout.write(
            self.style.SUCCESS(f"Роли синхронизированы. Суперпользователей обновлено: {added}.")
        )
