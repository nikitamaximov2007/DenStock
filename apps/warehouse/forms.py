from django import forms

from .models import StorageLocation


class StorageLocationForm(forms.ModelForm):
    class Meta:
        model = StorageLocation
        fields = [
            "name",
            "code",
            "barcode",
            "level",
            "purpose",
            "parent",
            "storage_allowed",
            "sort_order",
            "description",
            "capacity",
        ]
        help_texts = {
            "barcode": "Можно оставить пустым — будет создан автоматически как LOC:<код>.",
        }
