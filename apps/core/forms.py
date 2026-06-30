"""Слой 24 — общая форма загрузки изображения (используется catalog и inventory)."""
from django import forms

from .files import validate_image_upload


class ImageUploadForm(forms.Form):
    image = forms.FileField(label="Файл")
    caption = forms.CharField(label="Подпись", max_length=255, required=False)

    def clean_image(self):
        file = self.cleaned_data["image"]
        validate_image_upload(file)
        return file
