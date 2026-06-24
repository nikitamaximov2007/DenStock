from django import forms

from apps.warehouse.models import StorageLocation

from .models import PartItem


def _storage_locations():
    """Только места, где разрешено хранение остатка."""
    return StorageLocation.objects.filter(storage_allowed=True, is_active=True)


class PartItemCreateForm(forms.Form):
    """Единичное создание экземпляра из строки партии."""

    serial_number = forms.CharField(max_length=100, required=False, label="Серийный номер")
    current_location = forms.ModelChoiceField(
        queryset=_storage_locations(), required=False, label="Место хранения"
    )
    note = forms.CharField(max_length=255, required=False, label="Примечание")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["current_location"].queryset = _storage_locations()


class PartItemBulkForm(forms.Form):
    """Массовое создание N экземпляров (без серийников, без сканера)."""

    count = forms.IntegerField(min_value=1, label="Сколько создать")
    note = forms.CharField(max_length=255, required=False, label="Примечание")


class PartItemEditForm(forms.ModelForm):
    class Meta:
        model = PartItem
        fields = ["serial_number", "current_location", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["current_location"].queryset = _storage_locations()
