"""Layer 28 — формы поступления. Валидация бизнес-правил — в services."""
from django import forms

from apps.catalog.models import PartType
from apps.inventory.presentation import part_option_label, with_part_identity
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

from .models import Receipt, ReceiptLine


class ReceiptForm(forms.ModelForm):
    """Шапка документа: поставщик, дата, комментарий."""

    class Meta:
        model = Receipt
        fields = ["supplier", "received_at", "comment"]
        widgets = {
            "received_at": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True).order_by(
            "name"
        )
        self.fields["supplier"].empty_label = "(не выбран)"


class ReceiptLineForm(forms.ModelForm):
    """Позиция: деталь, количество, себестоимость, ячейка."""

    class Meta:
        model = ReceiptLine
        fields = ["part_type", "quantity", "unit_cost_rub", "location", "comment"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Деталь выбирается по названию + exact-артикулу, не только по имени.
        self.fields["part_type"].queryset = with_part_identity(
            PartType.objects.filter(is_active=True).select_related("category").order_by("name"),
            part_field="",
        )
        self.fields["part_type"].label_from_instance = part_option_label
        self.fields["location"].queryset = StorageLocation.objects.filter(
            is_active=True, storage_allowed=True
        ).order_by("code")
        self.fields["quantity"].widget.attrs.update({"min": "0.001", "step": "any"})
        self.fields["unit_cost_rub"].widget.attrs.update({"min": "0", "step": "0.01"})

    def clean_quantity(self):
        quantity = self.cleaned_data["quantity"]
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("Количество должно быть больше нуля.")
        return quantity

    def clean_unit_cost_rub(self):
        cost = self.cleaned_data["unit_cost_rub"]
        if cost is not None and cost < 0:
            raise forms.ValidationError("Себестоимость не может быть отрицательной.")
        return cost
