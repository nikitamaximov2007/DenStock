"""Адаптеры каталогов поставщиков.

Слой намеренно тонкий: разбор прайса BRP уже реализован и доказан на реальном
файле в `apps.brp.importer`, поэтому адаптер его ВЫЗЫВАЕТ, а не повторяет.
Второй парсер и вторая формула цены не создаются.

Добавить поставщика значит добавить один класс с тремя методами, а не менять
код рабочего процесса.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from pathlib import Path

from django.db.models import Max

BRP = "brp"


class CatalogAdapterError(RuntimeError):
    """Файл не разобран: понятная причина для пользователя."""


class CatalogAdapter:
    """Контракт адаптера каталога."""

    key = ""
    label = ""

    def check(self, path: Path) -> dict:
        """Разобрать файл БЕЗ записи и вернуть сводку. Обязан быть read-only."""
        raise NotImplementedError

    def apply(self, path: Path) -> dict:
        """Применить разобранный файл к справочнику."""
        raise NotImplementedError

    def fingerprint(self) -> str:
        """Слепок текущего состояния каталога для защиты от устаревшего предпросмотра."""
        raise NotImplementedError


def _summary_dict(summary) -> dict:
    """Сводка импортёра в JSON-совместимый вид.

    dataclasses.asdict() нельзя, он ломает Counter. Внутри asdict
    словарь-подкласс пересобирается как `Counter(последовательность пар)`, и
    ключами становятся кортежи вида ('OBS', 1). Поэтому поля читаются напрямую,
    а любые отображения приводятся к обычному словарю со строковыми ключами.
    """
    if is_dataclass(summary):
        data = {item.name: getattr(summary, item.name) for item in fields(summary)}
    else:
        data = dict(summary)
    result = {}
    for key, value in data.items():
        if isinstance(value, Mapping):
            result[key] = {str(inner): value[inner] for inner in value}
        elif isinstance(value, Decimal):
            result[key] = str(value)
        else:
            result[key] = value
    return result


class BrpCatalogAdapter(CatalogAdapter):
    key = BRP
    label = "BRP"

    def check(self, path: Path) -> dict:
        from apps.brp.importer import BrpImportError, import_catalog

        try:
            summary = import_catalog(str(path), commit=False)
        except BrpImportError as exc:
            raise CatalogAdapterError(str(exc)) from exc
        return _summary_dict(summary)

    def apply(self, path: Path) -> dict:
        from apps.brp.importer import BrpImportError, import_catalog

        try:
            summary = import_catalog(str(path), commit=True)
        except BrpImportError as exc:
            raise CatalogAdapterError(str(exc)) from exc
        return _summary_dict(summary)

    def fingerprint(self) -> str:
        """Слепок каталога BRP: сколько позиций и когда последняя правка.

        Этого достаточно, чтобы поймать «каталог поменяли между проверкой и
        применением»: любой импорт, ручная правка или удаление сдвигают либо
        счётчик, либо отметку времени.
        """
        from apps.brp.models import BrpCatalogPart

        aggregate = BrpCatalogPart.objects.aggregate(
            total=Max("pk"), touched=Max("updated_at")
        )
        count = BrpCatalogPart.objects.count()
        payload = f"{count}|{aggregate['total']}|{aggregate['touched']}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def inspect(self, path: Path, *, sample_rows: int = 5) -> dict:
        """Read-only разбор структуры книги: листы, заголовки, первые строки.

        Нужен, когда поставщик пришлёт файл другой формы: инспектор показывает
        фактическую структуру, вместо того чтобы парсер угадывал колонки.
        """
        return inspect_workbook(path, sample_rows=sample_rows)


def inspect_workbook(path, *, sample_rows: int = 5) -> dict:
    """Показать структуру xlsx, ничего не меняя и не загружая книгу целиком."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - зависимость есть в проекте
        raise CatalogAdapterError("Не установлен openpyxl.") from exc

    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        raise CatalogAdapterError("Ожидается файл .xlsx.")
    if not path.exists():
        raise CatalogAdapterError("Файл не найден.")
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - причина показывается пользователю
        raise CatalogAdapterError(f"Файл не читается как Excel: {exc}") from exc
    try:
        sheets = list(workbook.sheetnames)
        worksheet = workbook[sheets[0]]
        headers: list[str] = []
        samples: list[list[str]] = []
        # read_only-итератор не держит книгу в памяти: берём только начало.
        for index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
            values = ["" if cell is None else str(cell).strip() for cell in row]
            if index == 1:
                headers = values
            elif len(samples) < sample_rows:
                samples.append(values)
            else:
                break
        return {
            "sheets": sheets,
            "sheet": sheets[0],
            "headers": headers,
            "sample_rows": samples,
            "declared_max_row": worksheet.max_row,
            "declared_max_column": worksheet.max_column,
        }
    finally:
        workbook.close()


ADAPTERS: dict[str, CatalogAdapter] = {BRP: BrpCatalogAdapter()}


def get_adapter(catalog: str) -> CatalogAdapter:
    adapter = ADAPTERS.get(catalog)
    if adapter is None:
        raise CatalogAdapterError("Неизвестный каталог.")
    return adapter


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
