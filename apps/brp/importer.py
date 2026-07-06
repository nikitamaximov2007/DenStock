"""Layer 31 — импорт дилерского прайса BRP из Excel в справочник.

СТРОГО справочник: импорт не создаёт остатков, движений, поступлений и не
удаляет складские карточки. Идемпотентен: повторный запуск обновляет только
изменившиеся строки. 127 тысяч строк обрабатываются чанками через bulk-операции.

Формат файла (проверен на реальном прайсе):
- первый лист; строка 1 — заголовки; строка 2 — примечания (пустая в колонках
  данных); данные со строки 3;
- значимые колонки A..H: Material_No, Part_Desc, Last_Yr_Util, Status,
  РОЗНИЦА (USD), ОПТОВАЯ (USD), ЗАМЕНА НОМЕРА, ЗАМЕНА НОМЕРА;
- колонки правее H (легенда статусов) игнорируются;
- Material_No хранится СТРОКОЙ (ведущие нули не теряются), пробелы обрезаются,
  пустые ячейки нормализуются.
"""
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils import timezone

from apps.catalog.models import normalize_number

from .models import BrpCatalogPart

CHUNK_SIZE = 1000
DATA_COLUMNS = 8  # A..H

# Поля, которые синхронизируются из файла при обновлении существующей строки.
SYNC_FIELDS = (
    "part_desc", "last_year_util", "brp_status",
    "retail_price_usd", "wholesale_price_usd",
    "replacement_no_1", "replacement_no_2",
)
UPDATE_FIELDS = SYNC_FIELDS + (
    "material_no_norm", "replacement_no_1_norm", "replacement_no_2_norm",
    "source_file", "source_row", "import_batch", "updated_at",
)


class BrpImportError(Exception):
    """Файл не может быть разобран (нет файла/листа/колонок)."""


@dataclass
class ImportSummary:
    mode: str = "dry-run"
    total_rows_scanned: int = 0
    data_rows: int = 0
    created: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    skipped_empty: int = 0
    duplicates: int = 0
    unique_materials: int = 0
    with_retail_price: int = 0
    with_wholesale_price: int = 0
    with_replacement: int = 0
    status_counts: Counter = field(default_factory=Counter)


def _text(value) -> str:
    """Ячейка -> строка: None -> '', числа -> str без потери, пробелы обрезаны."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # Excel любит превращать номера в 460041.0
    return str(value).strip()


def _dec(value):
    """Ячейка -> Decimal или None (пустое/нечисловое)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None


def _row_dict(cells, row_no: int) -> dict:
    padded = list(cells[:DATA_COLUMNS]) + [None] * (DATA_COLUMNS - len(cells))
    return {
        "material_no": _text(padded[0]),
        "part_desc": _text(padded[1])[:255],
        "last_year_util": _text(padded[2])[:20],
        "brp_status": _text(padded[3])[:20],
        "retail_price_usd": _dec(padded[4]),
        "wholesale_price_usd": _dec(padded[5]),
        "replacement_no_1": _text(padded[6])[:40],
        "replacement_no_2": _text(padded[7])[:40],
        "source_row": row_no,
    }


def _differs(obj: BrpCatalogPart, row: dict) -> bool:
    return any(getattr(obj, name) != row[name] for name in SYNC_FIELDS)


def _apply(obj: BrpCatalogPart, row: dict, *, source_file: str, batch: str) -> None:
    for name in SYNC_FIELDS + ("source_row",):
        setattr(obj, name, row[name])
    obj.source_file = source_file
    obj.import_batch = batch
    # bulk_create/bulk_update не вызывают save(): нормализацию считаем сами.
    obj.material_no_norm = normalize_number(obj.material_no)
    obj.replacement_no_1_norm = normalize_number(obj.replacement_no_1)
    obj.replacement_no_2_norm = normalize_number(obj.replacement_no_2)
    obj.updated_at = timezone.now()


def _flush(chunk: list[dict], summary: ImportSummary, *,
           commit: bool, source_file: str, batch: str) -> None:
    keys = [row["material_no"] for row in chunk]
    existing = {
        obj.material_no: obj
        for obj in BrpCatalogPart.objects.filter(material_no__in=keys)
    }
    to_create, to_update = [], []
    for row in chunk:
        obj = existing.get(row["material_no"])
        if obj is None:
            obj = BrpCatalogPart(material_no=row["material_no"])
            _apply(obj, row, source_file=source_file, batch=batch)
            to_create.append(obj)
            summary.created += 1
        elif _differs(obj, row):
            _apply(obj, row, source_file=source_file, batch=batch)
            to_update.append(obj)
            summary.updated += 1
        else:
            summary.skipped_unchanged += 1
    if commit:
        if to_create:
            BrpCatalogPart.objects.bulk_create(to_create, batch_size=CHUNK_SIZE)
        if to_update:
            BrpCatalogPart.objects.bulk_update(
                to_update, UPDATE_FIELDS, batch_size=CHUNK_SIZE
            )


def import_catalog(path, *, commit: bool = False, sheet: str | None = None) -> ImportSummary:
    """Разобрать Excel и синхронизировать справочник. dry-run ничего не пишет."""
    import openpyxl

    path = Path(path)
    if not path.exists():
        raise BrpImportError(f"Файл не найден: {path}")
    try:
        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — любой битый xlsx -> понятная ошибка
        raise BrpImportError(f"Не удалось открыть Excel: {exc}") from exc

    try:
        worksheet = workbook[sheet] if sheet else workbook[workbook.sheetnames[0]]
        summary = ImportSummary(mode="commit" if commit else "dry-run")
        batch = timezone.now().strftime("%Y-%m-%d_%H-%M-%S")
        seen: set[str] = set()
        chunk: list[dict] = []

        # Строка 1 — заголовки; примечания и пустые строки отсеются по
        # пустому Material_No (строка 2 пуста в колонках данных).
        for row_no, cells in enumerate(
            worksheet.iter_rows(min_row=2, values_only=True), start=2
        ):
            summary.total_rows_scanned += 1
            row = _row_dict(cells, row_no)
            if not row["material_no"]:
                summary.skipped_empty += 1
                continue
            if row["material_no"] in seen:
                summary.duplicates += 1
                continue
            seen.add(row["material_no"])
            summary.data_rows += 1
            if row["brp_status"]:
                summary.status_counts[row["brp_status"]] += 1
            if row["retail_price_usd"] is not None:
                summary.with_retail_price += 1
            if row["wholesale_price_usd"] is not None:
                summary.with_wholesale_price += 1
            if row["replacement_no_1"] or row["replacement_no_2"]:
                summary.with_replacement += 1
            chunk.append(row)
            if len(chunk) >= CHUNK_SIZE:
                _flush(chunk, summary, commit=commit,
                       source_file=path.name, batch=batch)
                chunk = []
        if chunk:
            _flush(chunk, summary, commit=commit, source_file=path.name, batch=batch)
        summary.unique_materials = len(seen)
        return summary
    finally:
        workbook.close()
