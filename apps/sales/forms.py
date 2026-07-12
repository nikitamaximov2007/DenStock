from django import forms

from apps.inventory.models import StockLot
from apps.inventory.presentation import ExactLotChoiceField, with_part_identity

from .models import Reservation, Sale


def _available_lots():
    """Лоты для выбора: опция подписана названием + exact-артикулом детали."""
    return with_part_identity(
        StockLot.objects.filter(status=StockLot.Status.AVAILABLE)
        .select_related("part_type", "location")
        .order_by("part_type__name", "location__code")
    )


class ReservationForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = ["customer_name", "customer_phone", "comment", "expires_at"]
        widgets = {
            "expires_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["expires_at"].required = False
        self.fields["expires_at"].input_formats = [
            "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        ]


class AddItemForm(forms.Form):
    code = forms.CharField(
        label="Экземпляр (внутр. номер / штрихкод / серийник)", max_length=100
    )


class AddLotForm(forms.Form):
    lot = ExactLotChoiceField(label="Лот", queryset=StockLot.objects.none())
    quantity = forms.DecimalField(
        label="Количество", max_digits=12, decimal_places=3, min_value=0.001
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = _available_lots()


class SaleForm(forms.ModelForm):
    class Meta:
        model = Sale
        fields = ["customer_name", "customer_phone", "comment"]


class AddSaleItemForm(forms.Form):
    code = forms.CharField(
        label="Экземпляр (внутр. номер / штрихкод / серийник)", max_length=100
    )
    unit_price = forms.DecimalField(
        label="Цена продажи за ед. (₽)", max_digits=12, decimal_places=2, min_value=0
    )


class AddSaleLotForm(forms.Form):
    lot = ExactLotChoiceField(label="Лот", queryset=StockLot.objects.none())
    quantity = forms.DecimalField(
        label="Количество", max_digits=12, decimal_places=3, min_value=0.001
    )
    unit_price = forms.DecimalField(
        label="Цена продажи за ед. (₽)", max_digits=12, decimal_places=2, min_value=0
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = _available_lots()
