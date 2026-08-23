from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from .models import (
    Category,
    Manufacturer,
    PartBarcode,
    PartCompatibility,
    PartNumber,
    PartType,
    Unit,
    VehicleMake,
    VehicleModel,
    VehicleType,
)
from .services import ManualPartError, assert_barcode_is_free, find_parts_by_article


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "parent", "sort_order"]


class ManufacturerForm(forms.ModelForm):
    class Meta:
        model = Manufacturer
        fields = ["name", "country"]


class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = ["name", "short_name"]


class VehicleTypeForm(forms.ModelForm):
    class Meta:
        model = VehicleType
        fields = ["name", "sort_order"]


class VehicleMakeForm(forms.ModelForm):
    class Meta:
        model = VehicleMake
        fields = ["vehicle_type", "name"]


class VehicleModelForm(forms.ModelForm):
    class Meta:
        model = VehicleModel
        fields = ["vehicle_make", "name", "year_from", "year_to"]


class CommaDecimalField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return super().to_python(value)

    def prepare_value(self, value):
        if isinstance(value, Decimal) and value.is_finite():
            rendered = format(value, "f")
            return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
        return super().prepare_value(value)


class PriceSettingsForm(forms.Form):
    current_usd_rate = CommaDecimalField(
        label="Курс доллара",
        max_digits=10,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "step": "0.0001", "class": "form-control"}
        ),
    )
    brp_markup_percent = CommaDecimalField(
        label="Наценка BRP",
        max_digits=6,
        decimal_places=2,
        min_value=Decimal("0"),
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "step": "0.01", "class": "form-control"}
        ),
    )
    polaris_markup_percent = CommaDecimalField(
        label="Наценка Polaris",
        max_digits=6,
        decimal_places=2,
        min_value=Decimal("0"),
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "step": "0.01", "class": "form-control"}
        ),
    )

    def clean(self):
        cleaned = super().clean()
        for field in (
            "current_usd_rate",
            "brp_markup_percent",
            "polaris_markup_percent",
        ):
            value = cleaned.get(field)
            if value is not None and not value.is_finite():
                self.add_error(field, ValidationError("Введите конечное число."))
        return cleaned


class PartTypeForm(forms.ModelForm):
    class Meta:
        model = PartType
        fields = [
            "name",
            "category",
            "manufacturer",
            "unit",
            "tracking_mode",
            "description",
            "recommended_price",
            "min_price",
            "min_stock_level",
        ]


class PartNumberForm(forms.ModelForm):
    class Meta:
        model = PartNumber
        fields = ["value", "kind", "is_primary", "note"]


class PartBarcodeForm(forms.ModelForm):
    class Meta:
        model = PartBarcode
        fields = ["value", "note"]


class PartCompatibilityForm(forms.ModelForm):
    class Meta:
        model = PartCompatibility
        fields = ["vehicle_model", "year_from", "year_to", "note"]
class ManualPartForm(forms.Form):
    """Короткая форма для случая «детали нет в каталоге, а она нужна сейчас».

    Полная карточка спрашивает девять полей, среди них обязательную категорию,
    которой на новой системе может не быть ни одной: тогда добавить деталь
    нельзя вообще, пока кто-то не заведёт справочник. Оператору в середине
    приёмки или продажи столько заполнять нечем и некогда.

    Здесь спрашивается только то, что человек в этот момент действительно
    знает, а остальное подставляется. Дозаполнить карточку можно потом, в её
    обычном редактировании.
    """

    name = forms.CharField(
        label="Название",
        max_length=200,
        widget=forms.TextInput(
            attrs={"class": "form-control", "autofocus": "autofocus",
                   "placeholder": "Например: Ремень вариатора"}
        ),
    )
    article = forms.CharField(
        label="Артикул",
        max_length=100,
        required=False,
        help_text="Необязательно. С артикулом деталь находится по номеру,"
                  " без него - только по названию.",
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Например: 417300383"}
        ),
    )
    price = CommaDecimalField(
        label="Рекомендуемая цена, ₽",
        required=False,
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0"),
        help_text="Необязательно. Сколько просим с клиента; при продаже цену"
                  " ещё можно изменить. Себестоимость сюда не вводится: она"
                  " появится сама при первой приёмке.",
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "step": "0.01", "class": "form-control"}
        ),
    )
    manufacturer_name = forms.CharField(
        label="Производитель",
        max_length=150,
        required=False,
        help_text="Необязательно. У аналога это часто единственное, чем он"
                  " отличается от исходной детали на бумаге.",
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Например: XYZ"}
        ),
    )
    barcode = forms.CharField(
        label="Штрихкод",
        max_length=100,
        required=False,
        help_text="Необязательно. Отсканируйте прямо с коробки: потом её может"
                  " не оказаться под рукой.",
        widget=forms.TextInput(
            attrs={"class": "form-control", "autocomplete": "off",
                   "autocorrect": "off", "autocapitalize": "off",
                   "spellcheck": "false", "placeholder": "Считайте сканером"}
        ),
    )
    # Артикулы не уникальны и уникальными быть не могут, поэтому совпадение не
    # запрещается, а показывается: почти всегда оператору нужна уже заведённая
    # деталь, а не вторая такая же.
    confirm_duplicate = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, with_manufacturer: bool = False, **kwargs):
        """Производитель спрашивается только у аналога.

        У обычной детали он почти всегда очевиден из названия и только удлиняет
        форму. У аналога наоборот: артикул часто совпадает с исходной деталью, и
        завод - единственное, чем они различаются на бумаге.
        """
        super().__init__(*args, **kwargs)
        self.duplicates = []
        if not with_manufacturer:
            self.fields.pop("manufacturer_name")

    def clean_name(self):
        # Лишние пробелы схлопываются: иначе «Ремень» и «Ремень  » выглядят на
        # экране одинаково, а поиском находится только одна из них.
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise ValidationError("Укажите название детали.")
        return name

    def clean_article(self):
        return self.cleaned_data["article"].strip()

    def clean_barcode(self):
        """Штрихкод в модели уникален, поэтому занятый ловится сразу в поле.

        Так человек видит причину рядом с тем, что набрал, а не общим
        сообщением над формой.
        """
        value = self.cleaned_data["barcode"].strip()
        if value:
            try:
                assert_barcode_is_free(value)
            except ManualPartError as exc:
                raise ValidationError(str(exc)) from exc
        return value

    def clean_manufacturer_name(self):
        return " ".join(self.cleaned_data["manufacturer_name"].split())

    def clean(self):
        cleaned = super().clean()
        self.duplicates = list(find_parts_by_article(cleaned.get("article") or ""))
        return cleaned

    def needs_duplicate_confirmation(self) -> bool:
        """Совпадение номера - это повод посмотреть, а не отказ.

        Ошибкой формы оно намеренно не оформляется: экран красил бы обычную
        подсказку в цвет отказа, и оператор читал бы её как «я сделал
        что-то не так». Решение принимает человек, поэтому страница просто
        возвращается ещё раз, с предупреждением и списком найденного.
        """
        if not self.duplicates:
            return False
        return not self.cleaned_data.get("confirm_duplicate")
