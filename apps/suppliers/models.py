from django.db import models


class Supplier(models.Model):
    name = models.CharField("Название", max_length=200, unique=True)
    country = models.CharField("Страна", max_length=100, blank=True)
    contact_person = models.CharField("Контактное лицо", max_length=150, blank=True)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    email = models.EmailField("Email", blank=True)
    website = models.URLField("Сайт", blank=True)
    default_currency = models.CharField("Валюта по умолчанию", max_length=3, default="RUB")
    comment = models.TextField("Комментарий", blank=True)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Поставщик"
        verbose_name_plural = "Поставщики"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name
