from django import forms

from apps.catalog.models import VehicleType
from apps.inventory.models import StockLot

from .models import RepairOrder


class RepairOrderForm(forms.ModelForm):
    class Meta:
        model = RepairOrder
        fields = [
            "customer_name", "customer_phone", "vehicle_type",
            "vehicle_make", "vehicle_model", "vehicle_identifier",
            "problem_description", "comment",
        ]
        widgets = {
            "problem_description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicle_type"].required = False
        self.fields["vehicle_type"].queryset = VehicleType.objects.filter(
            is_active=True
        ).order_by("sort_order", "name")


class AddRepairItemForm(forms.Form):
    code = forms.CharField(
        label="Экземпляр (внутр. номер / штрихкод / серийник)", max_length=100
    )


class AddRepairLotForm(forms.Form):
    lot = forms.ModelChoiceField(label="Лот", queryset=StockLot.objects.none())
    quantity = forms.DecimalField(
        label="Количество", max_digits=12, decimal_places=3, min_value=0.001
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = (
            StockLot.objects.filter(status=StockLot.Status.AVAILABLE)
            .select_related("part_type", "location")
            .order_by("part_type__name", "location__code")
        )
