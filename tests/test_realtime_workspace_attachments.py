import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.customer_requests.attachments import AttachmentError, validate_attachment

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
)


def test_attachment_validation_accepts_real_png():
    result = validate_attachment(SimpleUploadedFile("screen.png", b"\x89PNG\r\n\x1a\n"))
    assert result.content_type == "image/png"
    assert result.filename == "screen.png"


@pytest.mark.parametrize("name", ["manual.pdf", "manual.PDF", "manual.Pdf"])
def test_attachment_validation_accepts_real_pdf_case_insensitively(name):
    result = validate_attachment(
        SimpleUploadedFile(name, MINIMAL_PDF, content_type="application/pdf")
    )
    assert result.content_type == "application/pdf"


def test_pdf_validation_ignores_browser_mime_and_restores_preinspected_stream():
    upload = SimpleUploadedFile("manual.pdf", MINIMAL_PDF, content_type="application/octet-stream")
    assert upload.read(4) == b"%PDF"
    result = validate_attachment(upload)
    assert result.content == MINIMAL_PDF
    assert upload.tell() == 0


@pytest.mark.parametrize(
    "name, content",
    [
        ("run.exe", b"MZ"),
        ("fake.pdf", b"<html>"),
        ("fake.pdf", b"\x89PNG\r\n\x1a\n"),
        ("fake.pdf", b"PK\x03\x04zip"),
        ("photo.jpg", b"not-an-image"),
        ("photo.png", MINIMAL_PDF),
    ],
)
def test_attachment_validation_rejects_disguised_or_executable_files(name, content):
    with pytest.raises(AttachmentError):
        validate_attachment(SimpleUploadedFile(name, content))


def test_oversized_valid_pdf_is_rejected():
    with pytest.raises(AttachmentError, match="10 МБ"):
        validate_attachment(
            SimpleUploadedFile("large.pdf", MINIMAL_PDF + b"x" * (10 * 1024 * 1024))
        )
