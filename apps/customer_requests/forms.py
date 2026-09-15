from django import forms
from django.contrib.auth import get_user_model

from .models import TelegramOperator


class TelegramOperatorForm(forms.ModelForm):
    class Meta:
        model = TelegramOperator
        fields = ["user", "telegram_user_id", "role"]
        labels = {
            "user": "Пользователь DenisStock",
            "telegram_user_id": "Telegram ID сотрудника",
            "role": "Роль в боте",
        }
        help_texts = {
            "telegram_user_id": "Число, которое бот показывает сотруднику по команде /whoami.",
        }
        error_messages = {
            "telegram_user_id": {"unique": "Этот Telegram ID уже привязан к другому сотруднику."},
            "user": {"unique": "У этого пользователя уже есть Telegram ID."},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = (
            get_user_model()
            .objects.filter(is_active=True, telegram_operator__isnull=True)
            .order_by("username")
        )

    def clean_telegram_user_id(self):
        value = self.cleaned_data["telegram_user_id"]
        if value is None or value <= 0:
            raise forms.ValidationError("Telegram ID должен быть положительным числом.")
        return value

    def clean_user(self):
        user = self.cleaned_data["user"]
        if not user.can_manage_sales:
            raise forms.ValidationError(
                "У пользователя нет доступа к заявкам клиентов: бот его не пустит."
            )
        return user
