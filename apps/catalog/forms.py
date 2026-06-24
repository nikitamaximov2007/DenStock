from django import forms

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
