from django import forms

from apps.inventory.models import StockLot

from .models import WriteOffDocument


class WriteOffForm(forms.ModelForm):
    class Meta:
        model = WriteOffDocument
        fields = ["reason", "comment"]


class AddWriteOffItemForm(forms.Form):
    code = forms.CharField(
        label="Экземпляр (внутр. номер / штрихкод / серийник)", max_length=100
    )


class AddWriteOffLotForm(forms.Form):
    lot = forms.ModelChoiceField(label="Лот", queryset=StockLot.objects.none())
    quantity = forms.DecimalField(
        label="Количество", max_digits=12, decimal_places=3, min_value=0.001
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Списать можно доступный или карантинный лот (брак/карантин).
        self.fields["lot"].queryset = (
            StockLot.objects.filter(
                status__in=[StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE]
            )
            .select_related("part_type", "location")
            .order_by("part_type__name", "location__code")
        )
