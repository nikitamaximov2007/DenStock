"""Validated operator attachment inputs shared by the messenger adapters."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED = {"image/png", "image/jpeg", "image/webp", "application/pdf"}
EXTENSIONS = {"png", "jpg", "jpeg", "webp", "pdf"}


class AttachmentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedAttachment:
    content: bytes
    filename: str
    content_type: str


def validate_attachment(upload) -> ValidatedAttachment:
    if upload is None:
        raise AttachmentError("Файл не выбран.")
    filename = os.path.basename(str(getattr(upload, "name", "")))
    filename = re.sub(r"[^A-Za-z0-9А-Яа-я._ -]", "_", filename).strip(" .")[:180]
    if not filename or "." not in filename:
        raise AttachmentError("У файла должно быть безопасное имя и расширение.")
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension not in EXTENSIONS:
        raise AttachmentError("Поддерживаются PNG, JPEG, WEBP и PDF.")
    content = upload.read(MAX_ATTACHMENT_BYTES + 1)
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("Файл не должен быть больше 10 МБ.")
    if content.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        detected = "image/webp"
    else:
        raise AttachmentError("Содержимое файла не соответствует поддерживаемому типу.")
    if detected not in ALLOWED:
        raise AttachmentError("Тип файла запрещён.")
    expected = {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(extension, f"image/{extension}")
    if expected != detected:
        raise AttachmentError("Расширение файла не соответствует его содержимому.")
    return ValidatedAttachment(content, filename, detected)
