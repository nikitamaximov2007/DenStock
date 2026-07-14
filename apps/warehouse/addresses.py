"""Layer 31/32.1 — адресное хранение до ящика/контейнера/ячейки.

Физическая структура склада Дениса: одна комната, около шести стеллажей.
Зоны по умолчанию НЕ используются: навигация идёт по номеру стеллажа.
Полный адрес кодируется в StorageLocation.code по конвенции (новых полей
у модели не появилось, существующая логика остатков не тронута):

    S01-L02-D03-C08  стеллаж 1, уровень 2, выдвижной ящик 3, ячейка 8
    S02-L01-B04-C02  стеллаж 2, уровень 1, коробка/контейнер 4, ячейка 2
    S04-L02          стеллаж 4, уровень 2 (крупная деталь на полке)

Буквы: S = стеллаж (shelving unit), L = уровень снизу вверх (level),
D = выдвижной ящик (drawer), B = коробка или контейнер (box/bin),
C = ячейка внутри ящика/контейнера (cell/compartment). L01 - самый нижний
уровень; ящики/контейнеры считаются слева направо, если стоять перед
стеллажом; ячейки - слева направо, ряд за рядом.

Обратная совместимость: старые адреса с зоной (A-S01-L02-D01-C01) и старыми
буквами K (контейнер) / X (коробка) остаются валидными кодами мест хранения —
они читаются и ищутся как раньше. Новые адреса букв K/X и зону не используют.
Одна деталь может лежать в нескольких адресах: остаток и так привязан к месту
(StockBalance/StockLot).
"""
from django.db import IntegrityError, transaction

from .models import StorageLocation
from .services import StorageLocationCreateError

# Вид хранения -> буква в адресе. Полки сегмента не добавляют.
# K и X — легаси-буквы старых адресов: новые адреса используют B.
STORAGE_KIND_CODES = {
    "drawer": "D",  # выдвижной ящик
    "container": "B",  # коробка или контейнер (box/bin)
    "box": "B",  # коробка или контейнер (box/bin)
    "shelf": None,  # полка
    "open_shelf": None,  # открытая полка
}


class AddressError(ValueError):
    """Некорректные составляющие адреса."""


def compose_address(
    zone: str,
    rack: int,
    level: int,
    *,
    kind: str | None = None,
    unit_no: int | None = None,
    cell_no: int | None = None,
) -> str:
    """Собрать полный адрес места хранения из составляющих.

    zone: НЕобязательный код зоны (пустая строка = без зоны, это новый
    формат по умолчанию); rack/level: номера стеллажа и уровня; kind +
    unit_no: вид (drawer/container/box) и номер ящика/контейнера/коробки;
    cell_no: номер ячейки внутри. Для полки kind/unit_no/cell_no опускаются.
    """
    zone = (zone or "").strip().upper()
    if rack < 1 or level < 1:
        raise AddressError("Номера стеллажа и уровня начинаются с 1.")
    parts = ([zone] if zone else []) + [f"S{int(rack):02d}", f"L{int(level):02d}"]
    if kind is not None and kind not in STORAGE_KIND_CODES:
        raise AddressError(f"Неизвестный вид хранения: {kind}")
    kind_code = STORAGE_KIND_CODES.get(kind) if kind else None
    if kind_code:
        if not unit_no or unit_no < 1:
            raise AddressError("Для ящика/контейнера нужен его номер (от 1).")
        parts.append(f"{kind_code}{int(unit_no):02d}")
    if cell_no:
        if not kind_code:
            raise AddressError("Ячейка указывается внутри ящика или контейнера.")
        parts.append(f"C{int(cell_no):02d}")
    return "-".join(parts)


def get_or_create_location(address: str, *, name: str = "") -> StorageLocation:
    """Место хранения по полному адресу (создаёт ячейку, если её ещё нет)."""
    location = StorageLocation.objects.filter(code__iexact=address).first()
    if location is not None:
        return location
    level = (
        StorageLocation.Level.CELL if "-C" in address else StorageLocation.Level.SHELF
    )
    try:
        # A savepoint makes an expected unique conflict safe for an outer counting transaction.
        with transaction.atomic():
            return StorageLocation.objects.create(
                code=address, name=name or address, level=level
            )
    except IntegrityError as exc:
        # A concurrent request may already have created the same code. A barcode conflict
        # cannot be retried as a location lookup and must be shown to the operator.
        location = StorageLocation.objects.filter(code__iexact=address).first()
        if location is not None:
            return location
        raise StorageLocationCreateError(
            "Не удалось создать ячейку: штрихкод уже используется другой ячейкой."
        ) from exc
