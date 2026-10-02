"""Telegram ↔ MAX business-function parity.

Every scenario below runs, unchanged, against both real adapters
(``telegram_bot.handle_update`` and ``max_bot.handle_update``): the same user
intention must leave the same DenisStock state. Assertions are on the
database (part photos, audit rows, receipts, contexts, stored messages), not
on message wording. Button labels are only used to find the next button,
exactly as a person would.
"""
from __future__ import annotations

import base64
import itertools
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO

import pytest
from django.core.files.base import ContentFile
from PIL import Image

from apps.catalog.models import (
    Category,
    PartPhotoUploadAudit,
    PartType,
    PartTypeImage,
    PublicPartPhoto,
    Unit,
)
from apps.customer_requests import max_bot, operator_console, telegram_bot
from apps.customer_requests.attachments import AttachmentError, validate_attachment
from apps.customer_requests.max_api import MaxBotApi
from apps.customer_requests.messengers import (
    consume_max_start,
    consume_telegram_start,
    issue_max_link,
    issue_telegram_link,
)
from apps.customer_requests.models import (
    CustomerRequest,
    MaxDeliveryStatus,
    MaxMessage,
    OperatorConversationContext,
    OwnerPhotoUploadContext,
    OwnerPhotoUploadReceipt,
    StaffMessengerBinding,
    TelegramMessage,
)
from apps.sales.models import Sale

from .max_fake import FAKE_MAX_TOKEN, FakeMaxServer, message_callback, message_created
from .test_telegram_customer_messaging import _request, build_part

_ids = itertools.count(5_000_000)

PHOTO_MENU = "Загрузка фото по продажам/ремонтам"


def _jpeg(color=(30, 80, 140)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="JPEG")
    return output.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@dataclass
class Reply:
    text: str
    buttons: dict  # label -> callback payload (inline buttons only)


class _NoDownloadMaxApi:
    def download_url(self, url):  # pragma: no cover - inline payloads never download
        raise AssertionError("inline attachment must not be downloaded")


class TelegramAdapter:
    name = "telegram"

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.chat_id = user_id  # private chat

    def bind_kwargs(self) -> dict:
        return {"provider": "telegram", "provider_user_id": self.user_id}

    @staticmethod
    def _reply(outgoing) -> Reply | None:
        items = [item for item in outgoing if item.text]
        if not items:
            return None
        item = items[-1]
        buttons = {}
        for row in (item.reply_markup or {}).get("inline_keyboard", []):
            for button in row:
                if "callback_data" in button:
                    buttons[button["text"]] = button["callback_data"]
        return Reply(item.text, buttons)

    def text_event(self, text: str) -> dict:
        update_id = next(_ids)
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "chat": {"id": self.chat_id, "type": "private"},
                "from": {"id": self.user_id, "is_bot": False, "username": "staff"},
                "text": text,
            },
        }

    def photo_event(self, content: bytes, *, filename="photo.jpg", document=False) -> dict:
        update_id = next(_ids)
        message = {
            "message_id": update_id,
            "chat": {"id": self.chat_id, "type": "private"},
            "from": {"id": self.user_id, "is_bot": False, "username": "staff"},
            "_content": base64.b64encode(content).decode("ascii"),
        }
        if document:
            message["document"] = {"file_id": f"doc-{update_id}", "file_name": filename,
                                   "mime_type": "application/pdf"}
        else:
            message["photo"] = [{"file_id": f"photo-{update_id}", "file_size": len(content)}]
        return {"update_id": update_id, "message": message}

    def deliver(self, event: dict, *, fail_download=False) -> Reply | None:
        def loader(message):
            if fail_download:
                raise AttachmentError("network")
            _file_id, filename = telegram_bot._telegram_attachment_descriptor(message)
            return validate_attachment(
                ContentFile(base64.b64decode(message["_content"]), name=filename)
            )

        return self._reply(telegram_bot.handle_update(event, attachment_loader=loader))

    def send(self, text: str) -> Reply | None:
        return self.deliver(self.text_event(text))

    def press_event(self, payload: str) -> dict:
        number = next(_ids)
        return {
            "update_id": number,
            "callback_query": {
                "id": f"cb{number}",
                "from": {"id": self.user_id},
                "data": payload,
                "message": {"message_id": 1, "chat": {"id": self.chat_id, "type": "private"}},
            },
        }

    def press(self, payload: str) -> Reply | None:
        return self.deliver(self.press_event(payload))


class MaxAdapter:
    name = "max"

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.chat_id = user_id + 10_000  # a MAX dialog id is not the user id

    def bind_kwargs(self) -> dict:
        return {"provider": "max", "provider_user_id": self.user_id,
                "delivery_chat_id": self.chat_id}

    def text_event(self, text: str) -> dict:
        return message_created(self.user_id, self.chat_id, text, mid=f"mid.parity{next(_ids)}")

    def photo_event(self, content: bytes, *, filename="photo.jpg", document=False) -> dict:
        event = message_created(self.user_id, self.chat_id, "", mid=f"mid.parity{next(_ids)}")
        payload = {"content_base64": base64.b64encode(content).decode("ascii")}
        if document:
            payload["filename"] = filename
        event["message"]["body"]["attachments"] = [
            {"type": "file" if document else "image", "payload": payload}
        ]
        return event

    def deliver(self, event: dict, *, fail_download=False) -> Reply | None:
        before = MaxMessage.objects.order_by("-pk").values_list("pk", flat=True).first() or 0

        def loader(body):
            if fail_download:
                raise AttachmentError("network")
            return max_bot.load_operator_attachment(_NoDownloadMaxApi(), body)

        max_bot.handle_update(event, attachment_loader=loader)
        row = MaxMessage.objects.filter(pk__gt=before).order_by("-pk").first()
        if row is None:
            return None
        buttons = {}
        for line in row.buttons or []:
            for button in line:
                buttons[button["text"]] = button["payload"]
        return Reply(row.text, buttons)

    def send(self, text: str) -> Reply | None:
        return self.deliver(self.text_event(text))

    def press_event(self, payload: str) -> dict:
        return message_callback(self.user_id, self.chat_id, payload)

    def press(self, payload: str) -> Reply | None:
        return self.deliver(self.press_event(payload))


ADAPTERS = [TelegramAdapter, MaxAdapter]


@pytest.fixture
def staff_world(db, django_user_model, settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    category = Category.objects.create(name="Паритет")
    unit, _ = Unit.objects.get_or_create(name="Паритет шт", defaults={"short_name": "шт"})
    part_a = PartType.objects.create(name="BALL BEARING", category=category, unit=unit)
    part_b = PartType.objects.create(name="OIL SEAL", category=category, unit=unit)
    sale = Sale.objects.create(
        number="S-PARITY", status=Sale.Status.COMPLETED, customer_name="Иванов Иван"
    )
    parts = {"items": [part_a, part_b]}
    monkeypatch.setattr(
        operator_console, "_photo_operation_lines", lambda operation, kind: parts["items"]
    )
    user = django_user_model.objects.create_superuser(username="parity-owner", password="x" * 12)

    def make(adapter_cls, user_id=7300):
        adapter = adapter_cls(user_id)
        binding = StaffMessengerBinding.objects.create(
            user=user, operator_key="DENIS", customer_visible_label="Денис",
            **adapter.bind_kwargs(),
        )
        return adapter, binding

    return {"make": make, "part_a": part_a, "part_b": part_b, "sale": sale, "user": user,
            "parts": parts}


def _open_part(adapter, part_label="BALL BEARING") -> Reply:
    feed = adapter.send(PHOTO_MENU)
    operation = next(payload for label, payload in feed.buttons.items() if "ПРОДАЖА" in label)
    card = adapter.press(operation)
    part_button = next(payload for label, payload in card.buttons.items() if part_label in label)
    return adapter.press(part_button)


def _state(part) -> dict:
    images = list(PartTypeImage.objects.filter(part=part).order_by("pk"))
    return {
        "active": [image.pk for image in images if image.is_active],
        "inactive": [image.pk for image in images if not image.is_active],
        "primary": [image.pk for image in images if image.is_active and image.is_primary],
        "published": PublicPartPhoto.objects.filter(
            part=part, status=PublicPartPhoto.Status.PUBLISHED
        ).count(),
        "audits": PartPhotoUploadAudit.objects.filter(image__part=part).count(),
    }


def _context(binding):
    return OwnerPhotoUploadContext.objects.filter(binding=binding).first()


# --- Photo acceptance matrix (scenarios 1-15) ----------------------------------------------


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_add_first_second_third_photo_then_done(adapter_cls, staff_world):
    adapter, binding = staff_world["make"](adapter_cls)
    part, other = staff_world["part_a"], staff_world["part_b"]

    prompt = _open_part(adapter)  # 1: part has no photos
    assert _context(binding).part_type_id == part.pk
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.UPLOAD
    assert "Отмена" in prompt.buttons

    first = adapter.deliver(adapter.photo_event(_jpeg()))
    assert first.text.startswith("Фото добавлено")
    for _ in range(2):  # 2, 3, 4: «Добавить ещё фото» then a photo, twice
        add = adapter.press(first.buttons["Добавить ещё фото"])
        assert _context(binding).mode == OwnerPhotoUploadContext.Mode.UPLOAD
        first = adapter.deliver(adapter.photo_event(_jpeg((10, 10, 10))))
        assert first.text.startswith("Фото добавлено"), add.text

    state = _state(part)
    assert len(state["active"]) == 3 and state["primary"] == state["active"][:1]
    assert state["published"] == 3 and state["audits"] == 3
    assert set(PartPhotoUploadAudit.objects.values_list("source", flat=True)) == {adapter.name}
    assert _state(other)["active"] == []  # the selected part only
    assert OwnerPhotoUploadReceipt.objects.filter(binding=binding).count() == 3

    adapter.press(first.buttons["Готово"])  # 8
    assert _context(binding) is None
    adapter.deliver(adapter.photo_event(_jpeg((200, 0, 0))))  # after «Готово»
    assert len(_state(part)["active"]) == 3
    assert CustomerRequest.objects.count() == 0


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_replace_one_of_several_photos_keeps_the_others(adapter_cls, staff_world):
    adapter, binding = staff_world["make"](adapter_cls)
    part = staff_world["part_a"]
    _open_part(adapter)
    reply = adapter.deliver(adapter.photo_event(_jpeg()))
    adapter.press(reply.buttons["Добавить ещё фото"])
    reply = adapter.deliver(adapter.photo_event(_jpeg((5, 5, 5))))
    before = _state(part)

    choose = adapter.press(reply.buttons["Заменить фото"])  # 5
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.REPLACE_SELECT
    adapter.press(choose.buttons["Фото 2"])
    assert _context(binding).target_image_id == before["active"][1]
    done = adapter.deliver(adapter.photo_event(_jpeg((90, 90, 90))))
    assert done.text.startswith("Фото добавлено")

    after = _state(part)
    assert before["active"][0] in after["active"]
    assert before["active"][1] in after["inactive"]
    assert len(after["active"]) == 2 and after["primary"] == before["primary"]


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_single_photo_replacement_confirm_and_cancel(adapter_cls, staff_world):
    adapter, binding = staff_world["make"](adapter_cls)
    part = staff_world["part_a"]
    _open_part(adapter)
    reply = adapter.deliver(adapter.photo_event(_jpeg()))
    original = _state(part)

    confirm = adapter.press(reply.buttons["Заменить фото"])
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.REPLACE_CONFIRM
    adapter.press(confirm.buttons["Отмена"])  # 6: cancel replacement
    assert _context(binding) is None
    assert _state(part) == original

    reply = _open_part(adapter)
    confirm = adapter.press(reply.buttons["Заменить фото"])
    adapter.press(confirm.buttons["Заменить"])  # 7: confirm replacement
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.REPLACE_UPLOAD
    adapter.deliver(adapter.photo_event(_jpeg((1, 2, 3))))
    after = _state(part)
    assert original["active"][0] in after["inactive"]
    assert len(after["active"]) == 1 and after["primary"] == after["active"]


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_resent_photo_event_is_applied_once(adapter_cls, staff_world):
    adapter, binding = staff_world["make"](adapter_cls)
    part = staff_world["part_a"]
    _open_part(adapter)
    event = adapter.photo_event(_jpeg())
    first = adapter.deliver(event)  # 9 and 15: the same messenger event twice
    adapter.deliver(event)
    assert first.text.startswith("Фото добавлено")
    assert len(_state(part)["active"]) == 1
    assert OwnerPhotoUploadReceipt.objects.filter(binding=binding).count() == 1


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_stale_target_token_and_stale_session_change_nothing(adapter_cls, staff_world):
    adapter, binding = staff_world["make"](adapter_cls)
    part = staff_world["part_a"]
    _open_part(adapter)
    reply = adapter.deliver(adapter.photo_event(_jpeg()))
    adapter.press(reply.buttons["Добавить ещё фото"])
    reply = adapter.deliver(adapter.photo_event(_jpeg((7, 7, 7))))
    choose = adapter.press(reply.buttons["Заменить фото"])
    stale_target = choose.buttons["Фото 2"]
    PartTypeImage.objects.filter(pk=_state(part)["active"][1]).update(is_active=False)
    before = _state(part)

    adapter.press(stale_target)  # 10: the selected photo no longer exists
    context = _context(binding)
    assert context.mode == OwnerPhotoUploadContext.Mode.MANAGE
    assert context.target_image_id is None
    adapter.deliver(adapter.photo_event(_jpeg((8, 8, 8))))
    assert _state(part) == before  # nothing replaced, nothing added

    OperatorConversationContext.objects.filter(binding=binding).update(
        updated_at=OperatorConversationContext.objects.get(binding=binding).updated_at
        - timedelta(seconds=1)
    )
    adapter.press(reply.buttons["Добавить ещё фото"])  # a button of an older session
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.MANAGE


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_another_staff_identity_cannot_use_foreign_buttons(adapter_cls, staff_world):
    adapter, binding = staff_world["make"](adapter_cls)
    reply = _open_part(adapter)
    other_user = staff_world["user"].__class__.objects.create_superuser(
        username="parity-other", password="x" * 12
    )
    other = adapter_cls(7399)
    StaffMessengerBinding.objects.create(
        user=other_user, operator_key="RIM", customer_visible_label="Рим", **other.bind_kwargs()
    )
    other.press(reply.buttons["Отмена"])  # 11: a token belonging to another binding
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.UPLOAD
    stranger = adapter_cls(7398)  # never paired
    stranger.press(reply.buttons["Отмена"])
    assert _context(binding) is not None


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_part_cannot_vanish_between_selection_and_upload(adapter_cls, staff_world):
    """12: an open photo session protects its part; the upload lands on that part."""
    from django.db.models.deletion import ProtectedError

    adapter, binding = staff_world["make"](adapter_cls)
    part = staff_world["part_b"]
    _open_part(adapter, part_label="OIL SEAL")
    assert _context(binding).part_type_id == part.pk
    with pytest.raises(ProtectedError):
        part.delete()
    adapter.deliver(adapter.photo_event(_jpeg()))
    assert len(_state(part)["active"]) == 1
    assert _state(staff_world["part_a"])["active"] == []


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_unsupported_file_and_failed_download_change_nothing(adapter_cls, staff_world):
    adapter, binding = staff_world["make"](adapter_cls)
    part = staff_world["part_a"]
    _open_part(adapter)
    adapter.deliver(adapter.photo_event(PDF, filename="scan.pdf", document=True))  # 13
    assert _state(part)["active"] == []
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.UPLOAD

    adapter.deliver(adapter.photo_event(_jpeg()), fail_download=True)  # 14
    assert _state(part)["active"] == []
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.UPLOAD
    assert OwnerPhotoUploadReceipt.objects.count() == 0

    adapter.deliver(adapter.photo_event(_jpeg()))  # the retry succeeds
    assert len(_state(part)["active"]) == 1


def test_both_adapters_converge_to_identical_photo_state(staff_world):
    """One script, both messengers, two parts: the resulting photo sets match."""
    results = []
    for adapter_cls, part, user_id in (
        (TelegramAdapter, staff_world["part_a"], 7310),
        (MaxAdapter, staff_world["part_b"], 7311),
    ):
        adapter, _binding = staff_world["make"](adapter_cls, user_id)
        reply = _open_part(adapter, part_label=part.name)
        reply = adapter.deliver(adapter.photo_event(_jpeg()))
        adapter.press(reply.buttons["Добавить ещё фото"])
        reply = adapter.deliver(adapter.photo_event(_jpeg((3, 3, 3))))
        choose = adapter.press(reply.buttons["Заменить фото"])
        adapter.press(next(
            payload for text, payload in choose.buttons.items() if text.startswith("Фото 1")
        ))
        reply = adapter.deliver(adapter.photo_event(_jpeg((4, 4, 4))))
        adapter.press(reply.buttons["Готово"])
        images = list(PartTypeImage.objects.filter(part=part).order_by("pk"))
        results.append(
            [(image.is_active, image.is_primary, image.sort_order) for image in images]
        )
    assert results[0] == results[1]


# --- MAX-only defects found by the audit ------------------------------------------------------


def test_max_staff_answers_are_stored_in_the_shape_the_max_sender_can_send(staff_world):
    """Regression: console answers were stored as ``{"inline_keyboard": ...}``.

    The sender iterated that dict and raised TypeError, so a MAX employee never
    received a menu, a card or a photo prompt (Telegram did).
    """
    adapter, _binding = staff_world["make"](MaxAdapter)
    adapter.send(PHOTO_MENU)
    server = FakeMaxServer()
    server.start()
    try:
        api = MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=2)
        worker = max_bot.MaxBotWorker(api, worker_id="parity", heartbeat_file="")
        worker.pacer.wait = lambda _chat_id: None
        worker.send_customer_messages()
    finally:
        server.stop()
    row = MaxMessage.objects.get(dedupe_key__startswith="operator:")
    assert row.delivery_status == MaxDeliveryStatus.SENT
    buttons = server.sent[-1]["attachments"][0]["payload"]["buttons"]
    assert any("ПРОДАЖА" in button["text"] for line in buttons for button in line)
    assert all(button["payload"].startswith("op:") for line in buttons for button in line)


def test_max_sender_still_delivers_rows_queued_in_the_old_envelope(db):
    from apps.customer_requests.max_api import inline_keyboard

    rows = [[{"text": "A", "payload": "op:m:x"}]]
    assert inline_keyboard({"inline_keyboard": rows}) == inline_keyboard(rows)


def test_max_redelivered_staff_press_is_not_applied_twice(staff_world):
    adapter, binding = staff_world["make"](MaxAdapter)
    _open_part(adapter)
    reply = adapter.deliver(adapter.photo_event(_jpeg()))
    add_press = adapter.press_event(reply.buttons["Добавить ещё фото"])
    adapter.deliver(add_press)
    adapter.deliver(adapter.photo_event(_jpeg((9, 9, 9))))
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.MANAGE
    adapter.deliver(add_press)  # MAX redelivers the old press after a 503
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.MANAGE


def test_max_redelivered_pairing_code_does_not_reach_customer_flow(db, django_user_model,
                                                                   settings):
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    user = django_user_model.objects.create_superuser(username="pair-max", password="x" * 12)
    code = operator_console.issue_pairing_token(user=user, label="Денис", created_by=user)
    adapter = MaxAdapter(7501)
    event = adapter.text_event(code)
    adapter.deliver(event)
    assert StaffMessengerBinding.objects.filter(provider="max", provider_user_id=7501).exists()
    count = MaxMessage.objects.count()
    adapter.deliver(event)
    assert MaxMessage.objects.count() == count
    assert not MaxMessage.objects.filter(direction=MaxMessage.Direction.CUSTOMER).exists()


# --- Requests, replies, roles ------------------------------------------------------------------


def _linked_request(channel: str, key: str):
    request = _request(build_part(), key=key, messenger=channel)
    if channel == CustomerRequest.Messenger.TELEGRAM:
        token = issue_telegram_link(request_id=request.pk).token
        consume_telegram_start(token=token, chat_id=88_001, user_id=88_001, username="client")
    else:
        token = issue_max_link(request_id=request.pk).token
        consume_max_start(token=token, chat_id=88_101, user_id=88_102)
    return request


def _outbound(request):
    model = (
        TelegramMessage
        if request.preferred_messenger == CustomerRequest.Messenger.TELEGRAM
        else MaxMessage
    )
    return list(
        model.objects.filter(
            conversation__request=request, direction=model.Direction.OPERATOR
        ).values("text", "operator_user_id", "operator_author_label")
    )


@pytest.mark.parametrize("channel", ["telegram", "max"])
@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_staff_reply_reaches_the_customer_with_the_same_attribution(
    adapter_cls, channel, staff_world
):
    adapter, binding = staff_world["make"](adapter_cls)
    request = _linked_request(channel, key=f"{adapter.name}{channel}".ljust(32, "P"))
    listing = adapter.send("Все заявки")
    card = adapter.press(listing.buttons[next(
        label for label in listing.buttons if request.reference in label
    )])
    adapter.press(card.buttons["Ответить"])
    adapter.send("Деталь есть, ждём вас.")
    # The responder introduction is still in flight: the same refusal in both.
    adapter.send("И ещё одно уточнение.")
    label = "Денис, владелец сервиса PRO-STORE"
    owner = staff_world["user"].pk
    assert _outbound(request) == [
        {"text": "Вам отвечает Денис, владелец сервиса PRO-STORE.", "operator_user_id": owner,
         "operator_author_label": label},
        {"text": "Деталь есть, ждём вас.", "operator_user_id": owner,
         "operator_author_label": label},
    ]
    adapter.send("/cancel")
    adapter.send("Этого клиенту не отправлять.")
    assert len(_outbound(request)) == 2


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_unpaired_account_gets_no_staff_capability(adapter_cls, staff_world):
    owner, binding = staff_world["make"](adapter_cls)
    reply = _open_part(owner)
    request = _linked_request("telegram", key=f"role{owner.name}".ljust(32, "R"))
    stranger = adapter_cls(7397)
    stranger.press(reply.buttons["Отмена"])
    stranger.send("Все заявки")
    stranger.send("Загрузка фото по продажам/ремонтам")
    assert _context(binding) is not None
    assert not OperatorConversationContext.objects.exclude(binding=binding).exists()
    assert _outbound(request) == []


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_staff_without_sales_permission_is_not_staff(adapter_cls, staff_world):
    adapter, binding = staff_world["make"](adapter_cls)
    reply = _open_part(adapter)
    binding.user.is_superuser = False
    binding.user.save(update_fields=["is_superuser"])
    binding.user.groups.clear()
    assert not binding.user.__class__.objects.get(pk=binding.user_id).can_manage_sales
    adapter.press(reply.buttons["Отмена"])
    assert _context(binding) is not None  # re-authorized on every press


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_paired_staff_cannot_press_customer_buttons(adapter_cls, staff_world):
    adapter, _binding = staff_world["make"](adapter_cls)
    before = CustomerRequest.objects.count()
    adapter.press("rc:1")  # a reorder confirmation button
    adapter.press("menu")
    adapter.press("purchases")
    assert CustomerRequest.objects.count() == before
    assert not MaxMessage.objects.filter(dedupe_key__startswith="selector:callback:").exists()


# --- Customer-side parity found by the audit ---------------------------------------------------


class _Customer:
    def __init__(self, adapter_cls, request):
        self.adapter = adapter_cls(88_001 if adapter_cls is TelegramAdapter else 88_102)
        if adapter_cls is MaxAdapter:
            self.adapter.chat_id = 88_101

    def stored_customer_messages(self, request):
        model = TelegramMessage if isinstance(self.adapter, TelegramAdapter) else MaxMessage
        return model.objects.filter(
            conversation__request=request, direction=model.Direction.CUSTOMER
        ).count()


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_typed_my_requests_is_navigation_not_a_message(adapter_cls, db, settings):
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    channel = "telegram" if adapter_cls is TelegramAdapter else "max"
    request = _linked_request(channel, key=f"nav{channel}".ljust(32, "N"))
    customer = _Customer(adapter_cls, request)
    for text in ("Мои заявки", "заявки", "Все заявки"):
        customer.adapter.send(text)
    assert customer.stored_customer_messages(request) == 0
    customer.adapter.send("Когда будет деталь?")
    assert customer.stored_customer_messages(request) == 1


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda cls: cls.name)
def test_customer_attachment_is_refused_as_a_whole(adapter_cls, db, settings):
    settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED = True
    channel = "telegram" if adapter_cls is TelegramAdapter else "max"
    request = _linked_request(channel, key=f"att{channel}".ljust(32, "T"))
    customer = _Customer(adapter_cls, request)
    event = customer.adapter.photo_event(_jpeg())
    if adapter_cls is TelegramAdapter:
        event["message"]["caption"] = "вот моя деталь"
    else:
        event["message"]["body"]["text"] = "вот моя деталь"
    customer.adapter.deliver(event)
    assert customer.stored_customer_messages(request) == 0
    assert PartTypeImage.objects.count() == 0


def test_max_sticker_or_location_is_not_a_part_photo(staff_world):
    adapter, binding = staff_world["make"](MaxAdapter)
    _open_part(adapter)
    event = adapter.photo_event(_jpeg())
    event["message"]["body"]["attachments"][0]["type"] = "sticker"
    adapter.deliver(event)
    assert _state(staff_world["part_a"])["active"] == []
    assert _context(binding).mode == OwnerPhotoUploadContext.Mode.UPLOAD
