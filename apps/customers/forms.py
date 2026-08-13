from django import forms

from .models import Customer


class CustomerForm(forms.ModelForm):
    """Карточка клиента. Телефон необязателен и не уникален."""

    class Meta:
        model = Customer
        fields = ["name", "phone", "comment"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Иванов Иван", "autofocus": True}),
            "phone": forms.TextInput(attrs={"placeholder": "+7 912 123-45-67"}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Укажите имя клиента.")
        return name


class CustomerSelectionMixin(forms.ModelForm):
    """Выбор клиента из справочника с мягким переходом.

    Новый предпочтительный поток: выбрать карточку, тогда имя и телефон
    подставляются из неё. Старый поток свободного ввода сохраняется, иначе
    сломалась бы совместимость с существующими сценариями и импортом.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "customer" in self.fields:
            self.fields["customer"].required = False
            self.fields["customer"].label = "Клиент из справочника"
            self.fields["customer"].empty_label = "Не выбран (ввести вручную)"
            self.fields["customer"].queryset = Customer.objects.all()
        if "customer_name" in self.fields:
            self.fields["customer_name"].required = False

    def clean(self):
        cleaned = super().clean()
        customer = cleaned.get("customer")
        if customer is not None:
            # Карточка выбрана: снимок документа берётся из неё.
            cleaned["customer_name"] = customer.name
            cleaned["customer_phone"] = customer.phone
        elif not (cleaned.get("customer_name") or "").strip():
            self.add_error(
                "customer_name",
                "Выберите клиента из справочника или укажите имя вручную.",
            )
        return cleaned
