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


class PriceSettingsForm(forms.Form):
    current_usd_rate = CommaDecimalField(
        label="Курс доллара",
        max_digits=10,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        widget=forms.NumberInput(attrs={"step": "0.0001", "class": "form-control"}),
    )
    brp_markup_percent = CommaDecimalField(
        label="Наценка BRP",
        max_digits=6,
        decimal_places=2,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.01", "class": "form-control"}),
    )
    polaris_markup_percent = CommaDecimalField(
        label="Наценка Polaris",
        max_digits=6,
        decimal_places=2,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.01", "class": "form-control"}),
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
