"""Layer 32 — формы: выбор адреса ячейки (через единый compose_address)."""
from django import forms

from apps.warehouse.addresses import AddressError, compose_address, get_or_create_location

PLACE_TYPE_CHOICES = [
    ("drawer", "Ящик"),
    ("container", "Контейнер"),
    ("box", "Коробка"),
    ("shelf", "Полка"),
    ("open_shelf", "Открытая полка"),
]
KIND_NEEDS_NUMBER = {"drawer", "container", "box"}


class CountingStartForm(forms.Form):
    """Выбор точного места хранения. Полный адрес собирает compose_address."""

    zone_code = forms.CharField(label="Зона", max_length=8)
    rack_number = forms.IntegerField(label="Стеллаж", min_value=1)
    level_number = forms.IntegerField(label="Уровень", min_value=1)
    place_type = forms.ChoiceField(label="Тип места", choices=PLACE_TYPE_CHOICES)
    place_number = forms.IntegerField(
        label="Номер ящика/контейнера", min_value=1, required=False
    )
    cell_number = forms.IntegerField(label="Ячейка", min_value=1, required=False)
    comment = forms.CharField(label="Комментарий", max_length=255, required=False)

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        kind = cleaned["place_type"]
        place_number = cleaned.get("place_number")
        if kind in KIND_NEEDS_NUMBER and not place_number:
            self.add_error("place_number", "Для ящика/контейнера/коробки нужен номер.")
            return cleaned
        try:
            address = compose_address(
                cleaned["zone_code"],
                cleaned["rack_number"],
                cleaned["level_number"],
                kind=kind,
                unit_no=place_number,
                cell_no=cleaned.get("cell_number"),
            )
        except AddressError as exc:
            raise forms.ValidationError(str(exc)) from exc
        cleaned["full_address"] = address
        return cleaned

    def resolve_location(self):
        """Существующее место по адресу или новое (code = полный адрес)."""
        return get_or_create_location(
            self.cleaned_data["full_address"],
            name=self.cleaned_data.get("comment") or self.cleaned_data["full_address"],
        )
