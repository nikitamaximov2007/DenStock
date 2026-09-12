"""Общие поля и виджеты форм (используются catalog, inventory, продажи, ремонты)."""
from django import forms

from .files import validate_image_upload
from .phones import canonical_phone_text

PHONE_PLACEHOLDER = "+7 900 123-45-67"
PHONE_HELP_TEXT = (
    "Российский номер: наберите его, «+7» подставится само. Можно вставить "
    "в любом виде: «89001234567», «+79001234567» или «9001234567». Иностранный "
    "номер оставляем как ввели."
)


class ImageUploadForm(forms.Form):
    image = forms.FileField(label="Файл")
    caption = forms.CharField(label="Подпись", max_length=255, required=False)

    def clean_image(self):
        file = self.cleaned_data["image"]
        validate_image_upload(file)
        return file


class PhoneInput(forms.TextInput):
    """Единое поле телефона: телефонная клавиатура на мобильном и та же маска.

    `type=tel` с `inputmode=tel` открывает на телефоне цифровую клавиатуру, а
    `data-phone-input` подключает общую маску (`static/shared/phone_input.js`),
    одну и ту же для DenisStock и публичной заявки PRO-STOR. Маска - только
    удобство: канон записи всё равно считает сервер (`canonical_phone_text`),
    поэтому с выключенным JS поле работает как обычный текст.
    """

    def __init__(self, attrs=None):
        defaults = {
            "type": "tel",
            "inputmode": "tel",
            "autocomplete": "tel",
            "placeholder": PHONE_PLACEHOLDER,
            "maxlength": "50",
            "data-phone-input": "ru",
        }
        defaults.update(attrs or {})
        super().__init__(attrs=defaults)


class PhoneFormMixin:
    """Приводит перечисленные поля телефона к каноническому виду при сохранении.

    Нормализация живёт в `apps.core.phones`, а не в каждой форме: иначе у
    продажи, резерва, ремонта и карточки клиента завелись бы четыре немного
    разных правила. Поля перечисляет сама форма в `phone_fields`.
    """

    phone_fields: tuple[str, ...] = ()

    def clean(self):
        cleaned = super().clean()
        for name in self.phone_fields:
            if name in cleaned:
                cleaned[name] = canonical_phone_text(cleaned[name])
        return cleaned
