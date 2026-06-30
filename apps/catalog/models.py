import re

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import BaseImage


def normalize_number(value: str) -> str:
    """Нормализация номера для поиска: без пробелов/дефисов/разделителей, в верхнем регистре."""
    return re.sub(r"[\s\-_./]", "", value or "").upper()


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


class PartType(Dictionary):
    """Карточка вида детали (НЕ физический экземпляр и НЕ остаток).

    Закупочной себестоимости здесь нет — она появится в партиях и остатках
    (слои 6–12). Цены продажи (рекомендуемая/минимальная) — справочные.
    """

    class TrackingMode(models.TextChoices):
        SERIAL = "serial", "Поштучный"
        BULK = "bulk", "Количественный"

    name = models.CharField("Название", max_length=200)
    category = models.ForeignKey(
        Category, verbose_name="Категория", on_delete=models.PROTECT, related_name="parts"
    )
    manufacturer = models.ForeignKey(
        Manufacturer,
        verbose_name="Производитель",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="parts",
    )
    unit = models.ForeignKey(
        Unit, verbose_name="Единица", on_delete=models.PROTECT, related_name="parts"
    )
    tracking_mode = models.CharField(
        "Режим учёта", max_length=10, choices=TrackingMode.choices, default=TrackingMode.SERIAL
    )
    description = models.TextField("Описание", blank=True)
    recommended_price = models.DecimalField(
        "Рекомендуемая цена", max_digits=12, decimal_places=2, null=True, blank=True
    )
    min_price = models.DecimalField(
        "Минимальная цена", max_digits=12, decimal_places=2, null=True, blank=True
    )
    min_stock_level = models.DecimalField(
        "Минимальный остаток", max_digits=12, decimal_places=3, default=0
    )

    class Meta:
        verbose_name = "Вид детали"
        verbose_name_plural = "Виды деталей"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        # Минимальная цена не может быть выше рекомендуемой, если заданы обе.
        if (
            self.recommended_price is not None
            and self.min_price is not None
            and self.min_price > self.recommended_price
        ):
            raise ValidationError(
                {"min_price": "Минимальная цена не может быть больше рекомендуемой."}
            )

    def can_change_tracking_mode(self) -> bool:
        """TODO (слои 9–12): запретить смену режима, если по детали уже есть
        остатки/экземпляры. Сейчас остатков нет — всегда True."""
        return True


class PartNumber(models.Model):
    class Kind(models.TextChoices):
        OEM = "oem", "OEM"
        ARTICLE = "article", "Артикул"
        ANALOG = "analog", "Аналог"
        INTERNAL_REF = "internal_ref", "Внутренний справочный"

    part = models.ForeignKey(PartType, on_delete=models.CASCADE, related_name="numbers")
    value = models.CharField("Значение", max_length=100)
    normalized_value = models.CharField(max_length=100, editable=False, db_index=True)
    kind = models.CharField("Тип", max_length=20, choices=Kind.choices, default=Kind.OEM)
    is_primary = models.BooleanField("Основной", default=False)
    note = models.CharField("Примечание", max_length=255, blank=True)

    class Meta:
        verbose_name = "Номер детали"
        verbose_name_plural = "Номера детали"
        ordering = ["kind", "value"]

    def __str__(self) -> str:
        return f"{self.value} ({self.get_kind_display()})"

    def save(self, *args, **kwargs):
        self.normalized_value = normalize_number(self.value)
        super().save(*args, **kwargs)


class PartBarcode(models.Model):
    part = models.ForeignKey(PartType, on_delete=models.CASCADE, related_name="barcodes")
    value = models.CharField("Штрихкод", max_length=100, unique=True)
    note = models.CharField("Примечание", max_length=255, blank=True)

    class Meta:
        verbose_name = "Заводской штрихкод"
        verbose_name_plural = "Заводские штрихкоды"
        ordering = ["value"]

    def __str__(self) -> str:
        return self.value


class PartTypeImage(BaseImage):
    """Слой 24 — типовое фото вида детали (иллюстрация каталога)."""

    upload_folder = "part-types"

    part = models.ForeignKey(PartType, on_delete=models.CASCADE, related_name="images")

    class Meta(BaseImage.Meta):
        abstract = False
        verbose_name = "Фото вида детали"
        verbose_name_plural = "Фото видов деталей"
        constraints = [
            # Не более одного активного главного фото на вид детали.
            models.UniqueConstraint(
                fields=["part"],
                condition=models.Q(is_primary=True, is_active=True),
                name="uniq_parttypeimage_primary_active",
            ),
        ]

    @property
    def owner_id(self):
        return self.part_id

    @property
    def siblings(self):
        return PartTypeImage.objects.filter(part_id=self.part_id)


class PartCompatibility(models.Model):
    part = models.ForeignKey(PartType, on_delete=models.CASCADE, related_name="compatibilities")
    vehicle_model = models.ForeignKey(
        VehicleModel,
        verbose_name="Модель техники",
        on_delete=models.PROTECT,
        related_name="compatibilities",
    )
    year_from = models.IntegerField("Год с", null=True, blank=True)
    year_to = models.IntegerField("Год по", null=True, blank=True)
    note = models.CharField("Комментарий", max_length=255, blank=True)

    class Meta:
        verbose_name = "Совместимость"
        verbose_name_plural = "Совместимость"
        constraints = [
            models.UniqueConstraint(
                fields=["part", "vehicle_model", "year_from", "year_to"],
                name="uniq_part_model_years",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.part} ↔ {self.vehicle_model}"
