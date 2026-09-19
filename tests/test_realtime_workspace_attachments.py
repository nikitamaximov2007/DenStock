import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.customer_requests.attachments import AttachmentError, validate_attachment


def test_attachment_validation_accepts_real_png():
    result = validate_attachment(SimpleUploadedFile("screen.png", b"\x89PNG\r\n\x1a\n"))
    assert result.content_type == "image/png"
    assert result.filename == "screen.png"


@pytest.mark.parametrize(
    "name, content",
    [("run.exe", b"MZ"), ("fake.pdf", b"<html>"), ("photo.jpg", b"not-an-image")],
)
def test_attachment_validation_rejects_disguised_or_executable_files(name, content):
    with pytest.raises(AttachmentError):
        validate_attachment(SimpleUploadedFile(name, content))
