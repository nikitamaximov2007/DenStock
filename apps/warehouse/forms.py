from django import forms

from .addresses import AddressError, compose_address, create_location
from .models import StorageLocation
from .services import (
    StorageLocationCreateError,
    StorageLocationRenameError,
    _require_cell_address,
    normalize_storage_location_code,
)


class StorageAddressV2CreateForm(forms.Form):
    """Новая ячейка по S/D/C. Стеллаж и ящик создаются автоматически, если их нет.

    Тип места, название, назначение и вместимость пользователю не показываются:
    ячейка получает значения модели по умолчанию (название = код, обычное
    назначение, без вместимости), как и прежде при пустых полях.
    """

    rack_number = forms.IntegerField(label="Стеллаж (S)", min_value=1)
    drawer_number = forms.IntegerField(label="Ящик (D)", min_value=0)
    cell_number = forms.IntegerField(label="Ячейка (C)", min_value=1)
    description = forms.CharField(
        label="Описание", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )

    conflict_location = None

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        try:
            cleaned["code"] = compose_address(
                cleaned["rack_number"],
                drawer_no=cleaned["drawer_number"],
                cell_no=cleaned["cell_number"],
            )
        except AddressError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return cleaned

    def save(self):
        try:
            return create_location(
                self.cleaned_data["code"],
                description=self.cleaned_data.get("description", ""),
            )
        except StorageLocationCreateError as exc:
            self.conflict_location = exc.location
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
            "barcode": "Можно оставить пустым - будет создан автоматически как LOC:<код>.",
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
    confirm = forms.BooleanField(required=False, widget=forms.HiddenInput)
    new_code = forms.CharField(label="Новый адрес ячейки", max_length=60)

    def clean_new_code(self):
        try:
            return _require_cell_address(self.cleaned_data["new_code"]).code
        except StorageLocationRenameError as exc:
            raise forms.ValidationError(str(exc)) from exc


class StorageDrawerRenameForm(forms.Form):
    expected_code = forms.CharField(widget=forms.HiddenInput)
    expected_fingerprint = forms.CharField(required=False, widget=forms.HiddenInput)
    new_number = forms.IntegerField(label="Новый номер ящика D", min_value=0)
