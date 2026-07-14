from django import forms

from .models import StorageLocation
from .services import StorageLocationRenameError, normalize_storage_location_code


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


class StorageLocationUpdateForm(StorageLocationForm):
    """Общие свойства ячейки редактируются отдельно от её физического кода."""

    class Meta(StorageLocationForm.Meta):
        fields = [
            "name",
            "barcode",
            "level",
            "purpose",
            "parent",
            "storage_allowed",
            "sort_order",
            "description",
            "capacity",
        ]


class StorageLocationRenameForm(forms.Form):
    expected_code = forms.CharField(widget=forms.HiddenInput)
    new_code = forms.CharField(label="Новый код ячейки", max_length=60)

    def clean_new_code(self):
        try:
            return normalize_storage_location_code(self.cleaned_data["new_code"])
        except StorageLocationRenameError as exc:
            raise forms.ValidationError(str(exc)) from exc
