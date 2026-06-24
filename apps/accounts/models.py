from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Кастомная модель пользователя.

    Ставится сразу на Слое 1: менять модель пользователя после старта проекта
    в Django крайне болезненно. Роли и права добавляются на Слое 2.
    Поля каталога/продаж тут не появляются — только идентификация пользователя.
    """

    full_name = models.CharField("ФИО", max_length=255, blank=True)

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"

    def __str__(self) -> str:
        return self.full_name or self.get_username()
