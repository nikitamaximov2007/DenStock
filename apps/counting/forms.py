"""Layer 32 — формы: выбор адреса ячейки (через единый compose_address).

Hotfix 32.1: зоны из формы убраны (склад — одна комната, навигация по номеру
стеллажа). Коробка и контейнер — одна буква B; старые адреса с зоной/K/X
остаются валидными, но новые так не создаются.
"""
from django import forms

from apps.warehouse.addresses import AddressError, compose_address, get_or_create_location

PLACE_TYPE_CHOICES = [
    ("drawer", "Выдвижной ящик (D)"),
    ("box", "Коробка или контейнер (B)"),
    ("shelf", "Полка (без ящика)"),
]
KIND_NEEDS_NUMBER = {"drawer", "container", "box"}


class CountingStartForm(forms.Form):
    """Выбор точного места хранения. Полный адрес собирает compose_address."""

    rack_number = forms.IntegerField(label="Стеллаж (S)", min_value=1)
    level_number = forms.IntegerField(label="Уровень снизу вверх (L)", min_value=1)
    place_type = forms.ChoiceField(label="Тип места", choices=PLACE_TYPE_CHOICES)
    place_number = forms.IntegerField(
        label="Номер ящика/коробки", min_value=1, required=False
    )
    cell_number = forms.IntegerField(label="Ячейка (C)", min_value=1, required=False)
    comment = forms.CharField(label="Описание ячейки", max_length=255, required=False)

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
                "",
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
