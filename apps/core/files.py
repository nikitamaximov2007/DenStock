"""Слой 24 — безопасная валидация и размещение загружаемых изображений.

Без Pillow и без сторонних зависимостей: проверяем расширение по allowlist, размер и
**сигнатуру файла (magic bytes)** — браузерному `content_type` и исходному имени файла
НЕ доверяем. На диск пишем под сгенерированным UUID-именем (см. `image_upload_to`).
"""
import os
import uuid

from django.core.exceptions import ValidationError

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 МБ
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _sniff(head: bytes) -> str | None:
    """Определить тип изображения по первым байтам. None — не jpg/png/webp."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def validate_image_upload(file) -> None:
    """Проверить загружаемый файл. Бросает ValidationError при нарушении правил."""
    ext = os.path.splitext(getattr(file, "name", "") or "")[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError("Разрешены только изображения JPG, JPEG, PNG или WEBP.")
    if file.size > MAX_IMAGE_SIZE:
        raise ValidationError("Файл слишком большой (максимум 10 МБ).")
    head = file.read(12)
    file.seek(0)
    kind = _sniff(head)
    if kind is None:
        raise ValidationError("Файл не является корректным изображением JPG/PNG/WEBP.")
    # Содержимое должно соответствовать расширению (jpg/jpeg → jpeg).
    ext_kind = "jpeg" if ext in {".jpg", ".jpeg"} else ext.lstrip(".")
    if ext_kind != kind:
        raise ValidationError("Расширение файла не соответствует его содержимому.")


def image_upload_to(instance, filename: str) -> str:
    """Путь хранения: <папка>/<id владельца>/<uuid>.<ext>. Имя файла — не от пользователя."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        ext = ".bin"  # подстраховка; валидатор формы это уже отсёк
    return f"{instance.upload_folder}/{instance.owner_id}/{uuid.uuid4().hex}{ext}"
