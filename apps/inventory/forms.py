from decimal import Decimal

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


class StockLotCreateForm(forms.Form):
    """Создание количественного лота из строки партии."""

    location = forms.ModelChoiceField(queryset=_storage_locations(), label="Место хранения")
    quantity = forms.DecimalField(
        min_value=Decimal("0.001"), max_digits=12, decimal_places=3, label="Количество"
    )
    note = forms.CharField(max_length=255, required=False, label="Примечание")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = _storage_locations()


class StockLotQuickForm(forms.Form):
    """Быстрый лот на весь оставшийся остаток строки (количество — автоматически)."""

    location = forms.ModelChoiceField(queryset=_storage_locations(), label="Место хранения")
    note = forms.CharField(max_length=255, required=False, label="Примечание")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = _storage_locations()


class StockLotEditForm(forms.Form):
    """Правка лота: место, количество, примечание (статус — отдельным действием)."""

    location = forms.ModelChoiceField(queryset=_storage_locations(), label="Место хранения")
    quantity = forms.DecimalField(
        min_value=Decimal("0.001"), max_digits=12, decimal_places=3, label="Количество"
    )
    note = forms.CharField(max_length=255, required=False, label="Примечание")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = _storage_locations()


# --- Слой 10: перемещение и корректировка через движения ---------------------


class MoveItemForm(forms.Form):
    """Перемещение экземпляра в другую ячейку."""

    to_location = forms.ModelChoiceField(queryset=_storage_locations(), label="Куда")
    comment = forms.CharField(max_length=255, required=False, label="Комментарий")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["to_location"].queryset = _storage_locations()


class MoveLotForm(forms.Form):
    """Перемещение лота целиком в другую ячейку."""

    to_location = forms.ModelChoiceField(queryset=_storage_locations(), label="Куда")
    comment = forms.CharField(max_length=255, required=False, label="Комментарий")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["to_location"].queryset = _storage_locations()


class AdjustLotForm(forms.Form):
    """Корректировка количества лота на ±. Комментарий (причина) обязателен."""

    delta = forms.DecimalField(
        max_digits=12, decimal_places=3, label="Изменение (±)",
        help_text="Положительное — приход, отрицательное — расход.",
    )
    comment = forms.CharField(max_length=255, required=True, label="Причина")
