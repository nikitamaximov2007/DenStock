from django import forms

from .models import Category, Manufacturer, Unit, VehicleMake, VehicleModel, VehicleType


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
