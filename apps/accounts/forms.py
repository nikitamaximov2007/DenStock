from django import forms
from django.contrib.auth.models import Group

from .models import User


class UserCreateForm(forms.ModelForm):
    password = forms.CharField(label="Пароль", widget=forms.PasswordInput)
    groups = forms.ModelMultipleChoiceField(
        label="Роли",
        queryset=Group.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = User
        fields = ["username", "full_name", "is_active", "groups"]

    def save(self, commit: bool = True) -> User:
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
            self.save_m2m()
        return user


class UserEditForm(forms.ModelForm):
    new_password = forms.CharField(
        label="Новый пароль (оставьте пустым, чтобы не менять)",
        widget=forms.PasswordInput,
        required=False,
    )
    groups = forms.ModelMultipleChoiceField(
        label="Роли",
        queryset=Group.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = User
        fields = ["username", "full_name", "is_active", "groups"]

    def __init__(self, *args, editing_self: bool = False, **kwargs):
        self.editing_self = editing_self
        super().__init__(*args, **kwargs)

    def clean_is_active(self):
        is_active = self.cleaned_data["is_active"]
        # Защита: администратор не может деактивировать сам себя.
        if self.editing_self and not is_active:
            raise forms.ValidationError("Нельзя деактивировать собственную учётную запись.")
        return is_active

    def save(self, commit: bool = True) -> User:
        user = super().save(commit=False)
        new_password = self.cleaned_data.get("new_password")
        if new_password:
            user.set_password(new_password)
        if commit:
            user.save()
            self.save_m2m()
        return user
