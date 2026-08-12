"""Форма выбора нового canonical S-D-C адреса ячейки."""

from django import forms

from apps.warehouse.addresses import AddressError, compose_address, get_or_create_location


class CountingStartForm(forms.Form):
    """Выбор точного места хранения. Полный адрес собирает compose_address."""

    rack_number = forms.IntegerField(label="Стеллаж (S)", min_value=1)
    drawer_number = forms.IntegerField(
        label="Ящик снизу вверх (D)", min_value=1
    )
    cell_number = forms.IntegerField(label="Ячейка (C)", min_value=1)
    comment = forms.CharField(label="Описание ячейки", max_length=255, required=False)

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        try:
            address = compose_address(
                cleaned["rack_number"],
                drawer_no=cleaned["drawer_number"],
                cell_no=cleaned["cell_number"],
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
