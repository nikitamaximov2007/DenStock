from django import forms

from apps.core.forms import PHONE_HELP_TEXT, PhoneFormMixin, PhoneInput

from .models import Customer
from .services import customers_by_recent_activity


class CustomerForm(PhoneFormMixin, forms.ModelForm):
    """Карточка клиента. Телефон необязателен и не уникален."""

    phone_fields = ("phone",)

    class Meta:
        model = Customer
        fields = ["name", "phone", "comment"]
        # Заметка о клиенте у оператора называется описанием и живёт в
        # существующем поле comment: заводить рядом второе поле того же смысла
        # незачем. Видно оно только в карточке и её правке.
        labels = {"comment": "Описание клиента"}
        help_texts = {"phone": PHONE_HELP_TEXT}
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Иванов Иван", "autofocus": True}),
            "phone": PhoneInput(),
            "comment": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Чем занимается, какая техника, особенности"}
            ),
        }

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Укажите имя клиента.")
        return name


class CustomerSelectionMixin(PhoneFormMixin, forms.ModelForm):
    """Выбор клиента из справочника с мягким переходом.

    Новый предпочтительный поток: выбрать карточку, тогда имя и телефон
    подставляются из неё. Старый поток свободного ввода сохраняется, иначе
    сломалась бы совместимость с существующими сценариями и импортом.
    """

    phone_fields = ("customer_phone",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "customer_phone" in self.fields:
            self.fields["customer_phone"].widget = PhoneInput(
                attrs={"maxlength": self.fields["customer_phone"].max_length or 50}
            )
            self.fields["customer_phone"].help_text = PHONE_HELP_TEXT
        if "customer" in self.fields:
            self.fields["customer"].required = False
            self.fields["customer"].label = "Клиент из справочника"
            self.fields["customer"].empty_label = "Не выбран (ввести вручную)"
            # Свежий клиент первым: продажу и ремонт почти всегда оформляют
            # на того, кто уже приходил.
            self.fields["customer"].queryset = customers_by_recent_activity(limit=None)
        if "customer_name" in self.fields:
            self.fields["customer_name"].required = False

    def clean(self):
        cleaned = super().clean()
        customer = cleaned.get("customer")
        if customer is not None:
            # Карточка выбрана: снимок документа берётся из неё как есть, уже
            # после канонизации ручного ввода в PhoneFormMixin. Переписывать
            # телефон карточки по пути в документ нельзя: снимок обязан
            # совпадать с карточкой, включая старую запись номера.
            cleaned["customer_name"] = customer.name
            cleaned["customer_phone"] = customer.phone
        elif not (cleaned.get("customer_name") or "").strip():
            self.add_error(
                "customer_name",
                "Выберите клиента из справочника или укажите имя вручную.",
            )
        return cleaned
