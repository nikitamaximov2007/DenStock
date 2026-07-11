from django import forms

from apps.inventory.presentation import part_option_label, with_part_identity

from .models import Batch, BatchLine


class BatchForm(forms.ModelForm):
    class Meta:
        model = Batch
        fields = [
            "supplier",
            "country",
            "currency",
            "exchange_rate",
            "order_number",
            "invoice_number",
            "ordered_at",
            "shipped_at",
            "arrived_at",
            "goods_total",
            "shipping_cost",
            "customs_cost",
            "commission_cost",
            "other_cost",
            "notes",
        ]
        widgets = {
            "ordered_at": forms.DateInput(attrs={"type": "date"}),
            "shipped_at": forms.DateInput(attrs={"type": "date"}),
            "arrived_at": forms.DateInput(attrs={"type": "date"}),
        }


class BatchLineForm(forms.ModelForm):
    class Meta:
        model = BatchLine
        fields = ["part_type", "quantity", "unit_cost_currency", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Деталь выбирается по названию + exact-артикулу, не только по имени.
        field = self.fields["part_type"]
        field.queryset = with_part_identity(field.queryset, part_field="")
        field.label_from_instance = part_option_label
