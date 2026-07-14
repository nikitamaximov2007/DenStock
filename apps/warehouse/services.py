"""Доменные операции со структурой склада."""
import re

from django.db import IntegrityError, transaction

from .models import StorageLocation, StorageLocationRenameHistory


class StorageLocationRenameError(ValueError):
    """Ошибка, которую можно показать пользователю формы переименования."""


class StorageLocationCreateError(ValueError):
    """Ожидаемая ошибка создания ячейки через адресный flow."""


_LOCATION_CODE_RE = re.compile(r"^[A-ZА-ЯЁ0-9]+(?:-[A-ZА-ЯЁ0-9]+)*$")


def normalize_storage_location_code(raw_code: str) -> str:
    """Нормализовать совместимый с существующими адресами код ячейки.

    Новые составные адреса собирает ``compose_address``. Эта проверка также
    сохраняет читаемость легаси-кодов вроде ``A`` и ``03``.
    """
    code = (raw_code or "").strip().upper()
    if not code:
        raise StorageLocationRenameError("Укажите новый код ячейки.")
    if len(code) > StorageLocation._meta.get_field("code").max_length:
        raise StorageLocationRenameError("Код ячейки слишком длинный.")
    if not _LOCATION_CODE_RE.fullmatch(code):
        raise StorageLocationRenameError(
            "Код ячейки может содержать буквы, цифры и дефисы без пробелов."
        )
    if code.isdigit() and len(code) > 2:
        raise StorageLocationRenameError(
            "Номер детали нельзя использовать как код ячейки."
        )
    return code


def auto_location_barcode(code: str) -> str:
    """Штрихкод, управляемый кодом ячейки."""
    return f"LOC:{code}"


def is_auto_location_barcode(barcode: str, code: str) -> bool:
    """Определить, можно ли безопасно обновить штрихкод при переименовании."""
    return not barcode or barcode == auto_location_barcode(normalize_storage_location_code(code))


def _persist_location_rename(
    location: StorageLocation,
    *,
    old_code: str,
    new_code: str,
    new_barcode: str | None,
    by,
) -> None:
    """Записать изменение кода и его аудит в одной транзакции."""
    updates = {"code": new_code}
    if new_barcode is not None:
        updates["barcode"] = new_barcode
    StorageLocation.objects.filter(pk=location.pk).update(**updates)
    StorageLocationRenameHistory.objects.create(
        location=location,
        old_code=old_code,
        new_code=new_code,
        renamed_by=by,
    )


@transaction.atomic
def rename_storage_location(
    location: StorageLocation,
    *,
    new_code: str,
    expected_code: str,
    by=None,
) -> StorageLocation:
    """Переименовать одну существующую ячейку, не меняя её идентичность.

    Связанные остатки и документы продолжают ссылаться на тот же primary key.
    Отдельный снимок ``WarehouseAction.location_code`` намеренно не меняется.
    """
    locked_location = StorageLocation.objects.select_for_update().get(pk=location.pk)
    if expected_code != locked_location.code:
        raise StorageLocationRenameError(
            "Код ячейки уже изменён другим пользователем. Обновите страницу."
        )

    old_code = locked_location.code
    old_barcode = locked_location.barcode
    normalized_code = normalize_storage_location_code(new_code)
    if normalized_code == normalize_storage_location_code(old_code):
        raise StorageLocationRenameError("Новый код совпадает с текущим кодом ячейки.")
    if (
        StorageLocation.objects.filter(code__iexact=normalized_code)
        .exclude(pk=locked_location.pk)
        .exists()
    ):
        raise StorageLocationRenameError("Ячейка с таким кодом уже существует.")

    new_barcode = (
        auto_location_barcode(normalized_code)
        if is_auto_location_barcode(old_barcode, old_code)
        else None
    )
    if new_barcode and (
        StorageLocation.objects.filter(barcode=new_barcode)
        .exclude(pk=locked_location.pk)
        .exists()
    ):
        raise StorageLocationRenameError(
            "Штрихкод для нового кода уже используется другой ячейкой."
        )

    try:
        # Savepoint leaves the outer transaction usable after a concurrent unique conflict.
        with transaction.atomic():
            _persist_location_rename(
                locked_location,
                old_code=old_code,
                new_code=normalized_code,
                new_barcode=new_barcode,
                by=by,
            )
    except IntegrityError as exc:
        raise StorageLocationRenameError(
            "Ячейка с таким кодом или штрихкодом уже существует."
        ) from exc

    locked_location.code = normalized_code
    if new_barcode is not None:
        locked_location.barcode = new_barcode
    return locked_location
