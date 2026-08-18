"""Рабочий процесс импорта каталога: загрузка, проверка, применение.

Правила, которые этот слой обязан держать:

* проверка (dry-run) НИЧЕГО не пишет в справочник;
* применить можно только успешно проверенную партию;
* между проверкой и применением файл и состояние каталога обязаны совпасть,
  иначе пользователь применил бы не то, что видел;
* повторная загрузка того же файла безопасна, а повторное применение той же
  партии отклоняется;
* склад импорт не трогает вообще.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .adapters import CatalogAdapterError, file_sha256, get_adapter
from .models import CatalogImportBatch

MAX_UPLOAD_BYTES = 80 * 1024 * 1024
STORAGE_SUBDIR = "catalog-imports"
logger = logging.getLogger(__name__)


class CatalogImportError(RuntimeError):
    """Понятная пользователю ошибка рабочего процесса."""


def _storage_root() -> Path:
    root = Path(settings.PRIVATE_MEDIA_ROOT).resolve() / STORAGE_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def stored_file_path(batch: CatalogImportBatch) -> Path:
    """Абсолютный путь файла партии внутри приватного хранилища.

    Путь собирается от корня хранилища и проверяется на выход за его пределы:
    имя из базы недоверенное.
    """
    if not batch.stored_path:
        raise CatalogImportError("Файл этой партии не сохранён.")
    root = _storage_root()
    path = (root / batch.stored_path).resolve()
    if root not in path.parents and path.parent != root:
        raise CatalogImportError("Некорректный путь файла партии.")
    return path


def save_upload(upload, *, catalog: str, by=None) -> CatalogImportBatch:
    """Сохранить загруженный файл в приватное хранилище и создать партию."""
    name = (getattr(upload, "name", "") or "").strip()
    if not name.lower().endswith(".xlsx"):
        raise CatalogImportError("Нужен файл Excel в формате .xlsx.")
    size = getattr(upload, "size", 0) or 0
    if size <= 0:
        raise CatalogImportError("Файл пустой.")
    if size > MAX_UPLOAD_BYTES:
        raise CatalogImportError("Файл слишком большой для загрузки.")
    get_adapter(catalog)  # неизвестный каталог отсекаем до записи файла

    stored_name = f"{timezone.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.xlsx"
    target = _storage_root() / stored_name
    with open(target, "wb") as handle:
        for chunk in upload.chunks():
            handle.write(chunk)

    return CatalogImportBatch.objects.create(
        catalog=catalog,
        status=CatalogImportBatch.Status.UPLOADED,
        source_filename=name[:255],
        source_sha256=file_sha256(target),
        source_size=target.stat().st_size,
        stored_path=stored_name,
        created_by=by,
    )


def run_check(batch: CatalogImportBatch) -> CatalogImportBatch:
    """Проверка файла: полный разбор без единой записи в справочник."""
    adapter = get_adapter(batch.catalog)
    path = stored_file_path(batch)
    if not path.exists():
        raise CatalogImportError("Файл партии больше не доступен, загрузите заново.")

    fingerprint = adapter.fingerprint()
    try:
        summary = adapter.check(path)
    except CatalogAdapterError as exc:
        _mark_check_failed(batch, str(exc))
        raise CatalogImportError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - user must not receive a raw 500
        logger.exception("Unexpected catalog check failure for batch %s", batch.pk)
        _mark_check_failed(batch, f"{type(exc).__name__}: {exc}")
        raise CatalogImportError("Не удалось проверить файл. Обратитесь к администратору.") from exc

    batch.summary = summary
    batch.catalog_fingerprint = fingerprint
    batch.status = CatalogImportBatch.Status.CHECKED
    batch.error_text = ""
    batch.checked_at = timezone.now()
    batch.save(
        update_fields=["summary", "catalog_fingerprint", "status", "error_text", "checked_at"]
    )
    return batch


def _mark_check_failed(batch: CatalogImportBatch, error: str) -> None:
    batch.status = CatalogImportBatch.Status.CHECK_FAILED
    batch.error_text = error[:2000]
    batch.checked_at = timezone.now()
    batch.save(update_fields=["status", "error_text", "checked_at"])


def apply_batch(batch: CatalogImportBatch, *, by=None) -> CatalogImportBatch:
    """Применить проверенную партию к справочнику.

    Перед записью заново доказывается, что применяется ровно то, что
    показывали: тот же файл по SHA-256 и тот же каталог по слепку состояния.
    """
    if batch.status == CatalogImportBatch.Status.APPLIED:
        raise CatalogImportError("Эта партия уже применена.")
    if batch.status != CatalogImportBatch.Status.CHECKED:
        raise CatalogImportError("Сначала выполните проверку файла.")

    try:
        with transaction.atomic():
            # One durable batch row serializes every apply for this catalog.
            # The current batch itself guarantees that the queryset is non-empty.
            CatalogImportBatch.objects.select_for_update().filter(
                catalog=batch.catalog
            ).order_by("pk").first()
            locked = CatalogImportBatch.objects.select_for_update().get(pk=batch.pk)
            if locked.status != CatalogImportBatch.Status.CHECKED:
                raise CatalogImportError("Эта партия уже применена.")
            adapter = get_adapter(locked.catalog)
            path = stored_file_path(locked)
            if not path.exists():
                raise CatalogImportError("Файл партии больше не доступен, загрузите заново.")
            if file_sha256(path) != locked.source_sha256:
                raise CatalogImportError("STALE_DRY_RUN: файл изменился после проверки.")
            if adapter.fingerprint() != locked.catalog_fingerprint:
                raise CatalogImportError(
                    "STALE_DRY_RUN: каталог изменился после проверки, повторите проверку."
                )
            summary = adapter.apply(path)
            locked.apply_summary = summary
            locked.status = CatalogImportBatch.Status.APPLIED
            locked.applied_at = timezone.now()
            locked.applied_by = by
            locked.error_text = ""
            locked.save(
                update_fields=[
                    "apply_summary", "status", "applied_at", "applied_by", "error_text",
                ]
            )
    except CatalogImportError:
        raise
    except CatalogAdapterError as exc:
        _mark_apply_failed(batch, str(exc))
        raise CatalogImportError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - пользователю не показываем traceback
        _mark_apply_failed(batch, f"{type(exc).__name__}: {exc}")
        raise CatalogImportError(
            "Импорт не применён: произошла ошибка, справочник не изменён."
        ) from exc

    batch.refresh_from_db()
    return batch


def _mark_apply_failed(batch: CatalogImportBatch, message: str) -> None:
    """Пометить неудачу отдельной транзакцией: состояние не должно быть неясным."""
    CatalogImportBatch.objects.filter(pk=batch.pk).update(
        status=CatalogImportBatch.Status.APPLY_FAILED,
        error_text=message[:2000],
    )


def previous_applied(batch: CatalogImportBatch):
    """Ранее применённая партия того же файла: подсказка про повторную загрузку."""
    return (
        CatalogImportBatch.objects.filter(
            catalog=batch.catalog,
            source_sha256=batch.source_sha256,
            status=CatalogImportBatch.Status.APPLIED,
        )
        .exclude(pk=batch.pk)
        .order_by("-applied_at")
        .first()
    )
