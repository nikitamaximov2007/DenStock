"""Безопасная валидация и размещение загружаемых изображений.

Расширение и MIME браузера не являются доказательством типа файла. Перед сохранением
мы открываем изображение Pillow, проверяем фактический формат и декодируем первый кадр.
Публичный pipeline всё равно перекодирует разрешённый источник в безопасный JPEG.
"""
import os
import uuid
import warnings

from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 МБ
ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".heic", ".heif", ".avif",
    ".bmp", ".tif", ".tiff", ".gif",
}
_EXTENSION_FORMATS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".png": "PNG", ".webp": "WEBP",
    ".heic": "HEIC", ".heif": "HEIF", ".avif": "AVIF", ".bmp": "BMP",
    ".tif": "TIFF", ".tiff": "TIFF", ".gif": "GIF",
}
SUPPORTED_IMAGE_FORMATS = frozenset(_EXTENSION_FORMATS.values())


def expected_image_format(extension: str) -> str | None:
    """Return the decoder format expected for a normalized file extension."""
    return _EXTENSION_FORMATS.get(str(extension or "").lower())


def validate_image_upload(file) -> None:
    """Validate and decode an uploaded still image.

    GIF is accepted only as a safe first-frame source. HEIC/HEIF/AVIF are accepted
    when the deployed Pillow build has a decoder for them; unsupported codecs fail
    closed at the decode step rather than being stored as arbitrary bytes.
    """
    ext = os.path.splitext(getattr(file, "name", "") or "")[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(
            "Разрешены только файлы изображений JPEG, PNG, WEBP, HEIC, HEIF, AVIF, BMP, TIFF и GIF."
        )
    if file.size > MAX_IMAGE_SIZE:
        raise ValidationError("Файл слишком большой (максимум 10 МБ).")
    expected = expected_image_format(ext)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(file) as image:
                if image.format != expected:
                    raise ValidationError("Расширение файла не соответствует его содержимому.")
                width, height = image.size
                if width < 1 or height < 1 or width * height > 40_000_000:
                    raise ValidationError("Размер изображения вне допустимых пределов.")
                image.seek(0)
                image.load()
    except ValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        raise ValidationError("Файл не читается как изображение.") from None
    finally:
        file.seek(0)


def image_upload_to(instance, filename: str) -> str:
    """Путь хранения: <папка>/<id владельца>/<uuid>.<ext>. Имя файла — не от пользователя."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        ext = ".bin"  # подстраховка; валидатор формы это уже отсёк
    return f"{instance.upload_folder}/{instance.owner_id}/{uuid.uuid4().hex}{ext}"
