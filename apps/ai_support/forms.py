from django import forms

from .models import DeveloperTicket, SupportRating


class MessageForm(forms.Form):
    text = forms.CharField(widget=forms.Textarea, strip=True)
    idempotency_token = forms.UUIDField(widget=forms.HiddenInput)
    route_path = forms.CharField(required=False, widget=forms.HiddenInput)
    browser_family = forms.CharField(required=False, max_length=20, widget=forms.HiddenInput)
    viewport = forms.CharField(required=False, max_length=20, widget=forms.HiddenInput)
    image = forms.FileField(required=False)
    image_consent = forms.BooleanField(required=False)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("image") and not cleaned.get("image_consent"):
            self.add_error(
                "image_consent", "Подтвердите отправку изображения внешнему провайдеру."
            )
        return cleaned


class RatingForm(forms.Form):
    value = forms.ChoiceField(choices=SupportRating.Value.choices)
    comment = forms.CharField(required=False, max_length=500)


class TicketForm(forms.Form):
    description = forms.CharField(max_length=4000, widget=forms.Textarea)
    question_message = forms.UUIDField(required=False)
    answer_message = forms.UUIDField(required=False)
    include_question = forms.BooleanField(required=False)
    include_answer = forms.BooleanField(required=False)
    include_screenshot = forms.BooleanField(required=False)
    include_diagnostics = forms.BooleanField(required=False)
    route_path = forms.CharField(required=False)
    browser_family = forms.CharField(required=False, max_length=20)
    viewport = forms.CharField(required=False, max_length=20)


class TicketStatusForm(forms.Form):
    status = forms.ChoiceField(choices=DeveloperTicket.Status.choices)
