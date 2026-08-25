from django import forms

from .addresses import AddressError, compose_address, create_location
from .models import StorageLocation
from .services import (
    StorageLocationCreateError,
    StorageLocationRenameError,
    normalize_storage_location_code,
)


class StorageAddressV2CreateForm(forms.Form):
    class LocationType:
        RACK = "rack"
        DRAWER = "drawer"
        CELL = "cell"

    location_type = forms.ChoiceField(
        label="Что создать",
        choices=[
            (LocationType.RACK, "Стеллаж"),
            (LocationType.DRAWER, "Ящик"),
            (LocationType.CELL, "Ячейка"),
        ],
    )
    rack_number = forms.IntegerField(label="Стеллаж (S)", min_value=1)
    drawer_number = forms.IntegerField(label="Ящик (D)", min_value=0, required=False)
    cell_number = forms.IntegerField(label="Ячейка (C)", min_value=1, required=False)
    name = forms.CharField(label="Название", max_length=150, required=False)
    purpose = forms.ChoiceField(
        label="Назначение",
        choices=StorageLocation.Purpose.choices,
        initial=StorageLocation.Purpose.NORMAL,
        required=False,
    )
    description = forms.CharField(
        label="Описание", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    capacity = forms.IntegerField(label="Вместимость", min_value=1, required=False)

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        location_type = cleaned["location_type"]
        drawer = cleaned.get("drawer_number")
        cell = cleaned.get("cell_number")
        if location_type in {self.LocationType.DRAWER, self.LocationType.CELL} and drawer is None:
            self.add_error("drawer_number", "Для ящика или ячейки укажите номер D.")
            return cleaned
        if location_type == self.LocationType.CELL and not cell:
            self.add_error("cell_number", "Для ячейки укажите номер C.")
            return cleaned
        try:
            cleaned["code"] = compose_address(
                cleaned["rack_number"],
                drawer_no=(drawer if location_type != self.LocationType.RACK else None),
                cell_no=(cell if location_type == self.LocationType.CELL else None),
            )
        except AddressError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return cleaned

    def save(self):
        try:
            return create_location(
                self.cleaned_data["code"],
                name=self.cleaned_data.get("name", ""),
                purpose=(
                    self.cleaned_data.get("purpose") or StorageLocation.Purpose.NORMAL
                ),
                description=self.cleaned_data.get("description", ""),
                capacity=self.cleaned_data.get("capacity"),
            )
        except StorageLocationCreateError as exc:
            raise forms.ValidationError(str(exc)) from exc


class StorageLocationForm(forms.ModelForm):
    class Meta:
        model = StorageLocation
        fields = [
            "name",
            "code",
            "barcode",
            "level",
            "purpose",
            "parent",
            "storage_allowed",
            "sort_order",
            "description",
            "capacity",
        ]
        help_texts = {
            "barcode": "Можно оставить пустым — будет создан автоматически как LOC:<код>.",
        }

    def clean_code(self):
        try:
            return normalize_storage_location_code(self.cleaned_data["code"])
        except StorageLocationRenameError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # Existing physical identities may only change through the rename service.
            self.fields.pop("code", None)
            self.fields.pop("barcode", None)


class StorageLocationUpdateForm(StorageLocationForm):
    """Общие свойства ячейки редактируются отдельно от её физического кода."""

    class Meta(StorageLocationForm.Meta):
        fields = [
            "name",
            "purpose",
            "storage_allowed",
            "sort_order",
            "description",
            "capacity",
        ]


class StorageLocationRenameForm(forms.Form):
    expected_code = forms.CharField(widget=forms.HiddenInput)
    next = forms.CharField(required=False, widget=forms.HiddenInput)
    new_code = forms.CharField(label="Новый код ячейки", max_length=60)

    def clean_new_code(self):
        try:
            return normalize_storage_location_code(self.cleaned_data["new_code"])
        except StorageLocationRenameError as exc:
            raise forms.ValidationError(str(exc)) from exc


class StorageDrawerRenameForm(forms.Form):
    expected_code = forms.CharField(widget=forms.HiddenInput)
    expected_fingerprint = forms.CharField(required=False, widget=forms.HiddenInput)
    new_number = forms.IntegerField(label="Новый номер ящика D", min_value=0)
