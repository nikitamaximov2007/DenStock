"""Безопасная валидация и размещение загружаемых изображений.

Имя и MIME файла приходят от клиента и не являются доказательством формата.
Поддерживаемый формат подтверждается безопасным декодированием Pillow и сигнатурой
содержимого. На диск пишем под сгенерированным UUID-именем.
"""
import os
import uuid
import warnings

from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 МБ
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_IMAGE_SUFFIXES = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}


class UnsupportedImageFormatError(ValueError):
    """The bytes decode, but not as one of the supported image formats."""


class InvalidImageError(ValueError):
    """The upload cannot be safely decoded as an image."""


class ImageTooLargeError(ValueError):
    """The decoded bitmap exceeds the safety pixel limit."""


def _sniff(head: bytes) -> str | None:
    """Определить тип изображения по первым байтам. None — не jpg/png/webp."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def inspect_supported_image(file, *, max_pixels: int = MAX_IMAGE_PIXELS) -> str:
    """Decode an upload and return its canonical format: ``jpeg``, ``png`` or ``webp``.

    The stream is rewound on every exit. The magic bytes narrow the accepted set,
    while Pillow decoding proves that the payload is a real readable image. A JPEG
    based container that Pillow labels more specifically is still accepted when its
    decoded bytes carry the JPEG signature.
    """
    try:
        file.seek(0)
        head = file.read(12)
        file.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(file) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > max_pixels:
                    raise ImageTooLargeError
                image.load()
                kind = _sniff(head)
                if kind is None:
                    raise UnsupportedImageFormatError
                return kind
    except (UnsupportedImageFormatError, ImageTooLargeError):
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise InvalidImageError from exc
    finally:
        file.seek(0)


def validate_image_upload(file) -> None:
    """Проверить загружаемый файл. Бросает ValidationError при нарушении правил."""
    ext = os.path.splitext(getattr(file, "name", "") or "")[1].lower()
    if ext and ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError("Можно загрузить только JPG, JPEG, PNG или WEBP.")
    if file.size > MAX_IMAGE_SIZE:
        raise ValidationError("Файл слишком большой (максимум 10 МБ).")
    try:
        kind = inspect_supported_image(file)
    except UnsupportedImageFormatError:
        raise ValidationError("Можно загрузить только JPG, JPEG, PNG или WEBP.") from None
    except ImageTooLargeError:
        raise ValidationError("Размер изображения вне допустимых пределов.") from None
    except InvalidImageError:
        raise ValidationError(
            "Не удалось прочитать изображение. Выберите корректный JPG, JPEG, PNG или WEBP."
        ) from None

    if ext:
        # Содержимое должно соответствовать расширению (jpg/jpeg → jpeg).
        ext_kind = "jpeg" if ext in {".jpg", ".jpeg"} else ext.lstrip(".")
        if ext_kind != kind:
            raise ValidationError("Расширение файла не соответствует его содержимому.")
    else:
        # Some browser/provider payloads preserve the visible stem but omit the
        # hidden Windows extension. Store the generated file with a safe suffix.
        name = getattr(file, "name", "") or "photo"
        file.name = f"{name}{SUPPORTED_IMAGE_SUFFIXES[kind]}"


def image_upload_to(instance, filename: str) -> str:
    """Путь хранения: <папка>/<id владельца>/<uuid>.<ext>. Имя файла — не от пользователя."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        ext = ".bin"  # подстраховка; валидатор формы это уже отсёк
    return f"{instance.upload_folder}/{instance.owner_id}/{uuid.uuid4().hex}{ext}"
