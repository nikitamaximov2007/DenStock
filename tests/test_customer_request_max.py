from decimal import Decimal

import pytest

from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.customer_requests.max_provider import MaxProvider, handle_max_start
from apps.customer_requests.messengers import MessengerLinkError, issue_max_link
from apps.customer_requests.models import CustomerRequest, CustomerRequestMessengerContact

from .test_customer_requests import _create


@pytest.fixture
def part(db):
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    unit = Unit.objects.get(name="Штука")
    manufacturer, _ = Manufacturer.objects.get_or_create(name="BRP")
    result = PartType.objects.create(
        name="ДЕТАЛЬ MAX",
        category=category,
        unit=unit,
        manufacturer=manufacturer,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("1000"),
    )
    PartNumber.objects.create(part=result, value="MAX-1", is_primary=True)
    return result


class RecordingProvider(MaxProvider):
    def __init__(self):
        self.chat_ids = []

    def send_start_acknowledgement(self, *, chat_id: str) -> None:
        self.chat_ids.append(chat_id)


def _max_request(part):
    request, _ = _create(part=part, key="m" * 32)
    request.preferred_messenger = CustomerRequest.Messenger.MAX
    request.save(update_fields=["preferred_messenger"])
    return request


def test_max_uses_the_same_opaque_one_time_link_domain(part):
    request = _max_request(part)
    issued = issue_max_link(request_id=request.pk)
    provider = RecordingProvider()

    result = handle_max_start(token=issued.token, chat_id="max-chat-42", provider=provider)

    assert result.accepted is True
    contact = CustomerRequestMessengerContact.objects.get(request=request)
    assert contact.channel == "max"
    assert contact.remote_chat_id == "max-chat-42"
    assert provider.chat_ids == ["max-chat-42"]
    assert handle_max_start(token=issued.token, chat_id="max-chat-42").accepted is False


def test_max_rejects_other_channel_and_cancelled_request(part):
    request, _ = _create(part=part, key="n" * 32)
    with pytest.raises(MessengerLinkError, match="другой"):
        issue_max_link(request_id=request.pk)

    request.preferred_messenger = CustomerRequest.Messenger.MAX
    request.status = CustomerRequest.Status.CANCELED
    request.save(update_fields=["preferred_messenger", "status"])
    with pytest.raises(MessengerLinkError, match="отменённой"):
        issue_max_link(request_id=request.pk)
