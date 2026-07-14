from decimal import Decimal

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import models


class StorageLocation(models.Model):
    """Место хранения — узел цифровой карты склада.

    level — физический уровень дерева; purpose — назначение места (они
    независимы). Остатки появятся на следующих слоях; здесь только структура.
    """

    class Level(models.TextChoices):
        WAREHOUSE = "warehouse", "Склад"
        ZONE = "zone", "Зона"
        RACK = "rack", "Стеллаж"
        SECTION = "section", "Секция"
        SHELF = "shelf", "Полка"
        CELL = "cell", "Ячейка"

    class Purpose(models.TextChoices):
        NORMAL = "normal", "Обычное"
        RECEIVING = "receiving", "Приёмка"
        QUARANTINE = "quarantine", "Карантин"
        WRITEOFF = "writeoff", "Списание"

    name = models.CharField("Название", max_length=150)
    code = models.CharField("Код", max_length=60, unique=True)
    barcode = models.CharField("Штрихкод", max_length=80, unique=True, blank=True)
    level = models.CharField("Уровень", max_length=20, choices=Level.choices, default=Level.CELL)
    purpose = models.CharField(
        "Назначение", max_length=20, choices=Purpose.choices, default=Purpose.NORMAL
    )
    parent = models.ForeignKey(
        "self",
        verbose_name="Родитель",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    storage_allowed = models.BooleanField("Можно хранить остаток", default=True)
    is_active = models.BooleanField("Активно", default=True)
    sort_order = models.PositiveIntegerField("Порядок", default=0)
    description = models.TextField("Описание", blank=True)
    capacity = models.PositiveIntegerField("Вместимость", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Место хранения"
        verbose_name_plural = "Места хранения"
        ordering = ["sort_order", "code"]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"

    def save(self, *args, **kwargs):
        if not self.barcode and self.code:
            self.barcode = f"LOC:{self.code}"
        super().save(*args, **kwargs)

    def clean(self) -> None:
        # Запрет циклов parent.
        if self.parent_id is None:
            return
        if self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": "Место не может быть родителем самого себя."})
        ancestor = self.parent
        while ancestor is not None:
            if ancestor.pk == self.pk:
                raise ValidationError({"parent": "Нельзя выбрать родителем своего потомка (цикл)."})
            ancestor = ancestor.parent

    @property
    def full_path(self) -> str:
        """Полный адрес по кодам: СКЛАД-1 / A / 03 / 02 / 04."""
        parts = []
        node = self
        while node is not None:
            parts.append(node.code)
            node = node.parent
        return " / ".join(reversed(parts))

    @property
    def full_name_path(self) -> str:
        parts = []
        node = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return " / ".join(reversed(parts))

    @property
    def depth(self) -> int:
        depth = 0
        ancestor = self.parent
        while ancestor is not None:
            depth += 1
            ancestor = ancestor.parent
        return depth

    def can_hold_stock(self) -> bool:
        """Можно ли класть остаток (инвариант слоёв 9–12)."""
        return self.is_active and self.storage_allowed


class StorageLocationRenameHistory(models.Model):
    """Неизменяемый аудит переименований физической ячейки."""

    location = models.ForeignKey(
        StorageLocation,
        verbose_name="Ячейка",
        on_delete=models.PROTECT,
        related_name="rename_history",
    )
    old_code = models.CharField("Старый код", max_length=60)
    new_code = models.CharField("Новый код", max_length=60)
    renamed_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        verbose_name="Кто переименовал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    renamed_at = models.DateTimeField("Переименовано", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "История переименования ячейки"
        verbose_name_plural = "История переименований ячеек"
        ordering = ["-renamed_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.old_code} -> {self.new_code}"


class ValuationSettings(models.Model):
    """Единый текущий курс для цен и финансовой оценки склада (одна строка).

    Это ТЕКУЩАЯ оценка повторного заказа и клиентских цен BRP/Polaris, а не
    бухгалтерская себестоимость уже купленных партий и не фактический
    исторический курс закупки.
    """

    current_usd_rate = models.DecimalField(
        "Текущий курс доллара (₽ за $)",
        max_digits=10, decimal_places=4, default=Decimal("105"),
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        verbose_name="Кто изменил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Настройки цен"
        verbose_name_plural = "Настройки цен"

    def __str__(self) -> str:
        return f"текущий курс {self.current_usd_rate} ₽/$"

    @classmethod
    def get(cls) -> "ValuationSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
