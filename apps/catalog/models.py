import re
import uuid

from django.conf import settings
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
    # Permanent external identity.  It is deliberately separate from the
    # warehouse primary key, which remains an implementation detail.
    public_id = models.UUIDField("Публичный ID", default=uuid.uuid4, unique=True, editable=False)
    is_public = models.BooleanField("Показывать в публичном каталоге", default=True)

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
        if self.pk:
            previous = (
                type(self).objects.filter(pk=self.pk).values_list("public_id", flat=True).first()
            )
            if previous and previous != self.public_id:
                raise ValidationError({"public_id": "Публичный ID нельзя изменять."})

    def can_change_tracking_mode(self) -> bool:
        """TODO (слои 9–12): запретить смену режима, если по детали уже есть
        остатки/экземпляры. Сейчас остатков нет — всегда True."""
        return True


class PartNumber(models.Model):
    class Kind(models.TextChoices):
        OEM = "oem", "OEM"
        ARTICLE = "article", "Артикул"
        # Значение остаётся прежним: меняется только подпись. Прежнее слово
        # «Аналог» здесь означало совсем другое - «эту же деталь могут
        # спросить под этим номером», - и стояло рядом с настоящими
        # аналогами-деталями. Термин взят из поиска, он уже был в продукте.
        ANALOG = "analog", "Вспомогательный номер"
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


class PublicPartPhoto(models.Model):
    """Решение человека о том, может ли внутреннее фото детали стать публичным.

    Кандидат - обычное фото вида детали (``PartTypeImage``). Наличие файла
    ничего не решает: пока менеджер каталога не опубликовал конкретное фото и
    не указал, откуда оно, публичный каталог его не видит. Строки без решения
    нет - значит, фото не публичное. Отказ тоже хранится, чтобы то же фото не
    возвращалось в очередь проверки.

    Публикация сохраняет пережатые копии (``PublicPartPhotoRendition``) без
    EXIF и ограниченного размера. Публичный процесс читает только их и только
    через базу: каталог ``MEDIA_ROOT`` в него не монтируется.
    """

    class Status(models.TextChoices):
        PUBLISHED = "published", "Опубликовано"
        REJECTED = "rejected", "Не показывать"

    class Source(models.TextChoices):
        OWN = "own", "Собственное фото"
        MANUFACTURER = "manufacturer", "Фото производителя"
        SUPPLIER = "supplier", "Фото поставщика"

    public_id = models.UUIDField("Публичный ID", default=uuid.uuid4, unique=True, editable=False)
    part = models.ForeignKey(
        PartType, verbose_name="Деталь", on_delete=models.CASCADE, related_name="public_photos"
    )
    source_image = models.OneToOneField(
        PartTypeImage,
        verbose_name="Внутреннее фото",
        on_delete=models.CASCADE,
        related_name="public_decision",
    )
    status = models.CharField("Статус", max_length=12, choices=Status.choices)
    source = models.CharField("Источник фото", max_length=20, choices=Source.choices, blank=True)
    source_note = models.CharField("Откуда фото (примечание)", max_length=255, blank=True)
    is_primary = models.BooleanField("Главное в каталоге", default=False)
    sort_order = models.PositiveIntegerField("Порядок", default=0)
    # Меняется вместе с копиями: входит в адрес картинки, чтобы браузер не
    # держал старую версию после повторной публикации.
    version = models.CharField("Версия копий", max_length=16, blank=True)
    confirmed_at = models.DateTimeField("Опубликовано", null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто опубликовал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    rejected_at = models.DateTimeField("Снято или отклонено", null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто снял",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Фото для публичного каталога"
        verbose_name_plural = "Фото для публичного каталога"
        ordering = ["-is_primary", "sort_order", "pk"]
        indexes = [
            models.Index(fields=["part", "status"], name="public_photo_part_status_idx"),
        ]
        constraints = [
            # Публикация без источника и без подтверждения невозможна даже в
            # обход формы: это правило стоит в базе.
            models.CheckConstraint(
                condition=~models.Q(status="published")
                | (~models.Q(source="") & models.Q(confirmed_at__isnull=False)),
                name="public_photo_published_has_provenance",
            ),
            models.CheckConstraint(
                condition=models.Q(status="published") | models.Q(is_primary=False),
                name="public_photo_primary_is_published",
            ),
            models.UniqueConstraint(
                fields=["part"],
                condition=models.Q(is_primary=True, status="published"),
                name="uniq_public_photo_primary",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.part} · {self.get_status_display()}"


class PublicPartPhotoRendition(models.Model):
    """Пережатая копия опубликованного фото, которую отдаёт публичный каталог."""

    class Variant(models.TextChoices):
        CARD = "card", "Карточка в поиске"
        DETAIL = "detail", "Страница детали"

    photo = models.ForeignKey(
        PublicPartPhoto, verbose_name="Фото", on_delete=models.CASCADE, related_name="renditions"
    )
    variant = models.CharField("Вариант", max_length=10, choices=Variant.choices)
    content_type = models.CharField("Тип", max_length=40)
    data = models.BinaryField("Данные")
    width = models.PositiveIntegerField("Ширина")
    height = models.PositiveIntegerField("Высота")
    byte_size = models.PositiveIntegerField("Размер, байт")
    sha256 = models.CharField("SHA-256", max_length=64)

    class Meta:
        verbose_name = "Копия публичного фото"
        verbose_name_plural = "Копии публичных фото"
        constraints = [
            models.UniqueConstraint(fields=["photo", "variant"], name="uniq_public_photo_variant"),
        ]

    def __str__(self) -> str:
        return f"{self.photo_id} · {self.variant}"


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
class PartAnalog(models.Model):
    """«Эта деталь - аналог вот этой»: две отдельные складские карточки.

    Не путать с номером вида «Аналог» у самой детали. Тот означает, что ОДНУ И
    ТУ ЖЕ деталь могут спросить под другим номером, и никакой второй карточки
    за ним нет. Здесь связаны разные карточки, у каждой свои остатки, партии,
    цена, штрихкоды и история.

    Артикул здесь ничего не решает. У аналога он часто совпадает с исходной
    деталью: на коробке пишут номер, под который деталь сделана. Одинаковый
    номер не делает две детали одной, поэтому уникальности по артикулу нет и
    быть не может.

    Связь направленная: «аналог для исходной». У одной исходной детали может
    быть много аналогов, и один аналог может подходить к нескольким исходным.
    Обратное направление отдельной записью не заводится: это тот же факт с
    другой стороны, и на экранах он показывается сам.
    """

    original = models.ForeignKey(
        PartType, verbose_name="Исходная деталь",
        on_delete=models.CASCADE, related_name="analog_links",
    )
    analog = models.ForeignKey(
        PartType, verbose_name="Аналог",
        on_delete=models.CASCADE, related_name="original_links",
    )
    note = models.CharField("Примечание", max_length=255, blank=True)
    source = models.CharField("Источник", max_length=120, default="internal")
    is_confirmed = models.BooleanField("Подтверждена для публичного каталога", default=False)
    confirmed_at = models.DateTimeField("Подтверждена", null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто подтвердил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто связал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Связь аналога"
        verbose_name_plural = "Связи аналогов"
        ordering = ["analog__name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["original", "analog"], name="uniq_part_analog_pair"
            ),
            # Деталь не может быть аналогом самой себя. Проверка стоит в базе,
            # потому что связь заводится не только из формы.
            models.CheckConstraint(
                condition=~models.Q(original=models.F("analog")),
                name="part_analog_not_self",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.analog} — аналог {self.original}"
