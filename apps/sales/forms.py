from decimal import Decimal

from django import forms

from apps.customers.forms import CustomerSelectionMixin
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


class ReservationForm(CustomerSelectionMixin):
    class Meta:
        model = Reservation
        fields = ["customer", "customer_name", "customer_phone", "comment", "expires_at"]
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


class SaleForm(CustomerSelectionMixin):
    class Meta:
        model = Sale
        fields = ["customer", "customer_name", "customer_phone", "comment"]


class SaleCancellationForm(forms.Form):
    reason = forms.CharField(label="Причина отмены", max_length=255)
    author = forms.CharField(label="Кто отменяет", max_length=255)

    def clean_reason(self):
        value = (self.cleaned_data["reason"] or "").strip()
        if not value:
            raise forms.ValidationError("Укажите причину отмены.")
        return value

    def clean_author(self):
        value = (self.cleaned_data["author"] or "").strip()
        if not value:
            raise forms.ValidationError("Укажите, кто отменяет документ.")
        return value


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


class SaleLineCancellationForm(forms.Form):
    """Сколько отменить, почему и кто отменяет.

    Верхняя граница считается сервером из остатка, доступного к сторнированию,
    и не берётся из строки продажи: часть могла уже вернуться.
    """

    quantity = forms.DecimalField(
        label="Количество отмены", max_digits=12, decimal_places=3, min_value=Decimal("0.001")
    )
    reason = forms.CharField(label="Причина", max_length=255)
    author = forms.CharField(label="Кто отменяет", max_length=255)

    def __init__(self, *args, remaining=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.remaining = remaining
        if remaining is not None:
            self.fields["quantity"].max_value = remaining
            # Decimal печатает хвостовые нули, а в поле нужен «4», а не «4.000».
            shown = format(Decimal(remaining).normalize(), "f")
            self.fields["quantity"].widget.attrs.update(
                {"min": "1", "max": shown, "step": "1"}
            )
            self.fields["quantity"].initial = 1

    def clean_quantity(self):
        quantity = self.cleaned_data["quantity"]
        if self.remaining is not None and quantity > self.remaining:
            raise forms.ValidationError(
                f"Доступно к отмене {format(Decimal(self.remaining).normalize(), 'f')}."
            )
        return quantity

    def clean_reason(self):
        return (self.cleaned_data.get("reason") or "").strip()

    def clean_author(self):
        return (self.cleaned_data.get("author") or "").strip()
