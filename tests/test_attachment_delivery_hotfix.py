import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import override_settings

from apps.customer_requests.attachments import (
    AttachmentStorageError,
    read_attachment,
)
from apps.customer_requests.max_api import MaxBotApi
from apps.customer_requests.storage import PrivateAttachmentStorage
from apps.customer_requests.telegram_api import TelegramBotApi


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def test_private_storage_writes_new_files_and_reads_legacy_files(tmp_path):
    legacy_root = tmp_path / "legacy"
    private_root = tmp_path / "private"
    with override_settings(MEDIA_ROOT=legacy_root, PRIVATE_MEDIA_ROOT=private_root):
        storage = PrivateAttachmentStorage()
        name = "customer_requests/sample.pdf"

        stored = storage.save(name, ContentFile(b"%PDF-test"))
        assert (private_root / stored).read_bytes() == b"%PDF-test"
        assert read_attachment(storage.open(stored)) == b"%PDF-test"

        legacy = FileSystemStorage(location=legacy_root)
        legacy_name = "customer_requests/legacy.pdf"
        legacy.save(legacy_name, ContentFile(b"legacy-bytes"))
        assert read_attachment(storage.open(legacy_name)) == b"legacy-bytes"


def test_unreadable_attachment_has_safe_worker_error():
    class Missing:
        def read(self):
            raise FileNotFoundError("/private/path/must-not-leak")

    with pytest.raises(AttachmentStorageError, match="недоступно"):
        read_attachment(Missing())


@pytest.mark.parametrize(
    ("content_type", "method", "field"),
    [("image/png", "sendPhoto", "photo"), ("image/jpeg", "sendPhoto", "photo"),
     ("image/webp", "sendPhoto", "photo"), ("application/pdf", "sendDocument", "document")],
)
def test_telegram_attachment_uses_correct_multipart_endpoint(content_type, method, field):
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = request.data
        seen["content_type"] = request.headers["Content-type"]
        return _Response(b'{"ok":true,"result":{"message_id":42}}')

    result = TelegramBotApi("test-token", opener=opener).send_file(
        chat_id=7,
        content=b"file-bytes",
        filename=f"sample.{content_type.rsplit('/', 1)[-1]}",
        content_type=content_type,
    )

    assert result["message_id"] == 42
    assert f"/{method}" in seen["url"]
    assert f'name="{field}"' .encode() in seen["body"]
    assert b"file-bytes" in seen["body"]
    assert seen["content_type"].startswith("multipart/form-data; boundary=")


@pytest.mark.parametrize(
    "content_type, kind", [("image/png", "image"), ("application/pdf", "file")]
)
def test_max_attachment_uses_upload_token_then_message(content_type, kind):
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if request.full_url.endswith(f"/uploads?type={kind}"):
            return _Response(b'{"url":"https://upload.invalid/file"}')
        if request.full_url == "https://upload.invalid/file":
            assert b"file-bytes" in request.data
            if kind == "image":
                return _Response(b'{"photos":{"opaque-photo":{"token":"upload-token"}}}')
            return _Response(b'{"token":"upload-token"}')
        assert request.full_url.endswith("/messages?chat_id=7&disable_link_preview=true")
        assert b'"token": "upload-token"' in request.data
        return _Response(b'{"message":{"body":{"mid":"mid.test"}}}')

    result = MaxBotApi("test-token", base_url="https://max.invalid", opener=opener).send_file(
        chat_id=7,
        content=b"file-bytes",
        filename="sample.pdf" if kind == "file" else "sample.png",
        content_type=content_type,
    )

    assert result["body"]["mid"] == "mid.test"
    assert len(requests) == 3
