from django.core.exceptions import ValidationError
from django.db import models


class Dictionary(models.Model):
    """Базовый справочник: активность вместо удаления + временные метки."""

    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Category(Dictionary):
    name = models.CharField("Название", max_length=150)
    parent = models.ForeignKey(
        "self",
        verbose_name="Родитель",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    sort_order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Категория"
        verbose_name_plural = "Категории"
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="uniq_category_parent_name"),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        # Запрет циклов: категория не может быть потомком самой себя.
        if self.parent_id is None:
            return
        if self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": "Категория не может быть родителем самой себя."})
        ancestor = self.parent
        while ancestor is not None:
            if ancestor.pk == self.pk:
                raise ValidationError({"parent": "Нельзя выбрать родителем своего потомка (цикл)."})
            ancestor = ancestor.parent

    @property
    def depth(self) -> int:
        depth = 0
        ancestor = self.parent
        while ancestor is not None:
            depth += 1
            ancestor = ancestor.parent
        return depth


class Manufacturer(Dictionary):
    name = models.CharField("Название", max_length=150, unique=True)
    country = models.CharField("Страна", max_length=100, blank=True)

    class Meta:
        verbose_name = "Производитель"
        verbose_name_plural = "Производители"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Unit(Dictionary):
    name = models.CharField("Название", max_length=50, unique=True)
    short_name = models.CharField("Сокращение", max_length=20)

    class Meta:
        verbose_name = "Единица измерения"
        verbose_name_plural = "Единицы измерения"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.short_name or self.name


class VehicleType(Dictionary):
    name = models.CharField("Название", max_length=100, unique=True)
    sort_order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Вид техники"
        verbose_name_plural = "Виды техники"
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class VehicleMake(Dictionary):
    vehicle_type = models.ForeignKey(
        VehicleType, verbose_name="Вид техники", on_delete=models.PROTECT, related_name="makes"
    )
    name = models.CharField("Марка", max_length=120)

    class Meta:
        verbose_name = "Марка техники"
        verbose_name_plural = "Марки техники"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle_type", "name"], name="uniq_make_type_name"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.vehicle_type})"


class VehicleModel(Dictionary):
    vehicle_make = models.ForeignKey(
        VehicleMake, verbose_name="Марка", on_delete=models.PROTECT, related_name="models"
    )
    name = models.CharField("Модель", max_length=150)
    year_from = models.IntegerField("Год с", null=True, blank=True)
    year_to = models.IntegerField("Год по", null=True, blank=True)

    class Meta:
        verbose_name = "Модель техники"
        verbose_name_plural = "Модели техники"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle_make", "name", "year_from", "year_to"],
                name="uniq_model_make_name_years",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vehicle_make.name} {self.name}"
