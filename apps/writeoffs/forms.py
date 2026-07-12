from django import forms

from apps.inventory.models import StockLot
from apps.inventory.presentation import ExactLotChoiceField, with_part_identity

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
    lot = ExactLotChoiceField(label="Лот", queryset=StockLot.objects.none())
    quantity = forms.DecimalField(
        label="Количество", max_digits=12, decimal_places=3, min_value=0.001
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Списать можно доступный или карантинный лот (брак/карантин).
        # Опция подписана названием + exact-артикулом детали.
        self.fields["lot"].queryset = with_part_identity(
            StockLot.objects.filter(
                status__in=[StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE]
            )
            .select_related("part_type", "location")
            .order_by("part_type__name", "location__code")
        )
