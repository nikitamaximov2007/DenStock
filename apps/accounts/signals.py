"""Гарантия: суперпользователь всегда состоит в группе «Администратор».

Дополняет вычисляемую возможность (superuser → все возможности) реальным
членством в группе, чтобы списки/фильтры по группам тоже это видели.
"""
from django.contrib.auth.models import Group
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import roles
from .models import User


@receiver(post_save, sender=User)
def ensure_superuser_in_admin_group(sender, instance: User, **kwargs) -> None:
    if not instance.is_superuser:
        return
    # Группа создаётся data-миграцией; если её ещё нет (ранние стадии) — выходим.
    group = Group.objects.filter(name=roles.ADMIN).first()
    if group and not instance.groups.filter(pk=group.pk).exists():
        instance.groups.add(group)
