from django import forms

from apps.warehouse.models import StorageLocation

from .models import StockReturn, StockReturnLine


class ReturnForm(forms.ModelForm):
    class Meta:
        model = StockReturn
        fields = ["reason", "comment"]


class AddReturnLineForm(forms.Form):
    source_line_id = forms.IntegerField(widget=forms.HiddenInput)
    to_location = forms.ModelChoiceField(
        label="Ячейка возврата", queryset=StorageLocation.objects.none()
    )
    restock_status = forms.ChoiceField(
        label="Состояние", choices=StockReturnLine.RestockStatus.choices
    )
    quantity = forms.DecimalField(
        label="Количество", max_digits=12, decimal_places=3, min_value=0.001, required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["to_location"].queryset = (
            StorageLocation.objects.filter(is_active=True, storage_allowed=True).order_by("code")
        )


class ReturnLineRestockStatusForm(forms.Form):
    """Change only the future physical state of a draft return line."""

    restock_status = forms.ChoiceField(
        label="Состояние", choices=StockReturnLine.RestockStatus.choices
    )
