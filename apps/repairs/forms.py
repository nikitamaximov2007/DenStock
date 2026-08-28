from decimal import Decimal

from django import forms

from apps.catalog.models import VehicleType
from apps.customers.forms import CustomerSelectionMixin
from apps.inventory.models import StockLot
from apps.inventory.presentation import ExactLotChoiceField, with_part_identity

from .models import RepairOrder


class RepairOrderForm(CustomerSelectionMixin):
    class Meta:
        model = RepairOrder
        fields = [
            "customer",
            "customer_name",
            "customer_phone",
            "vehicle_type",
            "vehicle_make",
            "vehicle_model",
            "vehicle_identifier",
            "problem_description",
            "comment",
        ]
        widgets = {
            "problem_description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicle_type"].required = False
        self.fields["vehicle_type"].queryset = VehicleType.objects.filter(is_active=True).order_by(
            "sort_order", "name"
        )


class RepairCancellationForm(forms.Form):
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


class RepairLineCancellationForm(forms.Form):
    """Количество из конкретной строки ремонта и её аудит отмены."""

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
        value = (self.cleaned_data.get("reason") or "").strip()
        if not value:
            raise forms.ValidationError("Укажите причину отмены.")
        return value

    def clean_author(self):
        value = (self.cleaned_data.get("author") or "").strip()
        if not value:
            raise forms.ValidationError("Укажите, кто отменяет.")
        return value


class AddRepairItemForm(forms.Form):
    code = forms.CharField(label="Экземпляр (внутр. номер / штрихкод / серийник)", max_length=100)
    customer_unit_price_rub = forms.DecimalField(
        label="Цена (₽)",
        max_digits=12,
        decimal_places=2,
        required=False,
        min_value=0,
    )


class AddRepairLotForm(forms.Form):
    lot = ExactLotChoiceField(label="Лот", queryset=StockLot.objects.none())
    quantity = forms.DecimalField(
        label="Количество", max_digits=12, decimal_places=3, min_value=0.001
    )
    customer_unit_price_rub = forms.DecimalField(
        label="Цена (₽)",
        max_digits=12,
        decimal_places=2,
        required=False,
        min_value=0,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Опция подписана названием + exact-артикулом детали (не только именем).
        self.fields["lot"].queryset = with_part_identity(
            StockLot.objects.filter(status=StockLot.Status.AVAILABLE)
            .select_related("part_type", "location")
            .order_by("part_type__name", "location__code")
        )
