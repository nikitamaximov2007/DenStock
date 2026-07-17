import hashlib
import io
import os
import uuid
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError

MAX_PIXELS = 20_000_000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
FORMATS = {
    "JPEG": {"extensions": {".jpg", ".jpeg"}, "mime": "image/jpeg", "suffix": ".jpg"},
    "PNG": {"extensions": {".png"}, "mime": "image/png", "suffix": ".png"},
    "WEBP": {"extensions": {".webp"}, "mime": "image/webp", "suffix": ".webp"},
}


@dataclass(frozen=True)
class NormalizedImage:
    content: bytes
    mime_type: str
    suffix: str
    width: int
    height: int
    sha256: str


def _open_image(upload):
    upload.seek(0)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image = Image.open(upload)
        if image.format not in FORMATS:
            raise ValidationError("Разрешены только изображения JPG, PNG или WEBP.")
        if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
            raise ValidationError("Анимированные и многостраничные изображения запрещены.")
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
            raise ValidationError("Изображение превышает ограничение 20 мегапикселей.")
        image.load()
        return image.copy(), image.format, width, height


def normalize_image(upload) -> NormalizedImage:
    if not upload:
        raise ValidationError("Изображение не выбрано.")
    max_bytes = min(settings.AI_SUPPORT_MAX_IMAGE_BYTES, MAX_IMAGE_BYTES)
    if upload.size <= 0 or upload.size > max_bytes:
        raise ValidationError("Размер изображения должен быть не более 5 МБ.")
    extension = os.path.splitext(upload.name or "")[1].lower()
    try:
        image, actual_format, width, height = _open_image(upload)
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValidationError("Файл повреждён или не является безопасным изображением.") from exc
    spec = FORMATS[actual_format]
    if extension not in spec["extensions"]:
        raise ValidationError("Расширение файла не соответствует изображению.")
    if (upload.content_type or "").lower() != spec["mime"]:
        raise ValidationError("MIME-тип файла не соответствует изображению.")

    output = io.BytesIO()
    if actual_format == "JPEG":
        image.convert("RGB").save(output, "JPEG", quality=90, optimize=True)
    elif actual_format == "PNG":
        mode = "RGBA" if "A" in image.getbands() else "RGB"
        image.convert(mode).save(output, "PNG", optimize=True)
    else:
        mode = "RGBA" if "A" in image.getbands() else "RGB"
        image.convert(mode).save(output, "WEBP", quality=90, method=6)
    content = output.getvalue()
    if not content or len(content) > max_bytes:
        raise ValidationError("После безопасной обработки изображение превышает 5 МБ.")
    return NormalizedImage(
        content=content,
        mime_type=spec["mime"],
        suffix=spec["suffix"],
        width=width,
        height=height,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _private_root() -> Path:
    return Path(settings.PRIVATE_MEDIA_ROOT).resolve()


def save_normalized_image(image: NormalizedImage) -> str:
    today = date.today()
    relative = Path("ai-support") / f"{today:%Y}" / f"{today:%m}" / (
        f"{uuid.uuid4().hex}{image.suffix}"
    )
    root = _private_root()
    target = (root / relative).resolve()
    if root not in target.parents:
        raise ValidationError("Недопустимый приватный путь.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(image.content)
    return relative.as_posix()


def private_path(relative_path: str) -> Path:
    root = _private_root()
    target = (root / relative_path).resolve()
    if root not in target.parents:
        raise FileNotFoundError
    return target


def delete_private_file(relative_path: str) -> bool:
    try:
        path = private_path(relative_path)
    except FileNotFoundError:
        return False
    if not path.is_file():
        return False
    path.unlink()
    return True
