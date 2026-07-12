from django import forms

from apps.inventory.models import StockLot
from apps.inventory.presentation import ExactLotChoiceField, with_part_identity
from apps.warehouse.models import StorageLocation

from .models import InventoryCountDocument

# Лоты, физически присутствующие на складе (их и инвентаризируем).
_PHYSICAL_LOT_STATUSES = [
    StockLot.Status.AVAILABLE,
    StockLot.Status.QUARANTINE,
    StockLot.Status.RECEIVING,
]


class InventoryCountForm(forms.ModelForm):
    class Meta:
        model = InventoryCountDocument
        fields = ["scope_location", "comment"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scope_location"].required = False
        self.fields["scope_location"].queryset = (
            StorageLocation.objects.filter(is_active=True, storage_allowed=True).order_by("code")
        )


class AddCountLotForm(forms.Form):
    lot = ExactLotChoiceField(label="Лот", queryset=StockLot.objects.none())

    def __init__(self, *args, location=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Опция подписана названием + exact-артикулом детали.
        qs = (
            StockLot.objects.filter(status__in=_PHYSICAL_LOT_STATUSES)
            .select_related("part_type", "location")
            .order_by("location__code", "part_type__name")
        )
        if location is not None:
            qs = qs.filter(location=location)
        self.fields["lot"].queryset = with_part_identity(qs)


class CountQuantityForm(forms.Form):
    counted = forms.DecimalField(
        label="Факт", max_digits=12, decimal_places=3, min_value=0
    )
