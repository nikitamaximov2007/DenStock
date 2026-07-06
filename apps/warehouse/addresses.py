"""Layer 31 — адресное хранение до ящика/контейнера/ячейки.

Физическая структура склада Дениса: Зона -> Стеллаж -> Уровень -> Ящик или
Контейнер -> Ячейка. Полный адрес кодируется в StorageLocation.code по
конвенции (новых полей у модели не появилось, существующая логика остатков
не тронута):

    B-S01-L02-D03-C08  зона B, стеллаж 1, уровень 2, ящик 3, ячейка 8
    A-S02-L01-K04-C02  зона A, стеллаж 2, уровень 1, контейнер 4, ячейка 2
    D-S01-L04          зона D, стеллаж 1, уровень 4 (крупная деталь на полке)

Буквы вида хранения: D = ящик (drawer), K = контейнер, X = коробка (box);
полка/открытая полка сегмента вида не имеют. Одна деталь может лежать в
нескольких адресах: остаток и так привязан к месту (StockBalance/StockLot).
"""
from .models import StorageLocation

# Вид хранения -> буква в адресе. Полки сегмента не добавляют.
STORAGE_KIND_CODES = {
    "drawer": "D",  # ящик
    "container": "K",  # пластиковый контейнер
    "box": "X",  # коробка
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

    zone: буква/код зоны (A, B, D...); rack/level: номера стеллажа и уровня;
    kind + unit_no: вид (drawer/container/box) и номер ящика/контейнера;
    cell_no: номер ячейки внутри. Для полки kind/unit_no/cell_no опускаются.
    """
    zone = (zone or "").strip().upper()
    if not zone:
        raise AddressError("Не указана зона.")
    if rack < 1 or level < 1:
        raise AddressError("Номера стеллажа и уровня начинаются с 1.")
    parts = [zone, f"S{int(rack):02d}", f"L{int(level):02d}"]
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
    return StorageLocation.objects.create(
        code=address, name=name or address, level=level
    )
