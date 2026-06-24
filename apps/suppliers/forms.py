from django import forms

from .models import Supplier


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = [
            "name",
            "country",
            "contact_person",
            "phone",
            "email",
            "website",
            "default_currency",
            "comment",
        ]
