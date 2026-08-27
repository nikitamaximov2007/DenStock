"""Откуда деталь известна складу: реальный остаток или только каталог.

Карточка, приехавшая из каталога аналогов, выглядит как обычная складская:
есть имя, производитель, артикул и цена. Остатка при этом нет - деталь никто
не принимал и ни в какую ячейку не клал. Называть такую «На складе» нельзя:
оператор идёт за ней и не находит.

Подпись считается только по фактам - есть ли физический остаток и есть ли
запись в каталоге аналогов. Ради подписи не создаётся ни лот, ни движение,
ни привязка к ячейке.
"""

from apps.inventory.models import PartItem, StockLot
from apps.inventory.services import ITEM_PHYSICAL_STATUSES, LOT_PHYSICAL_STATUSES

WAREHOUSE = "warehouse"
AFTERMARKET_CATALOG = "aftermarket_catalog"

LABELS = {
    WAREHOUSE: "На складе",
    AFTERMARKET_CATALOG: "Каталог аналогов",
}


def aftermarket_part_ids(part_ids) -> set[int]:
    """Какие из деталей заведены каталогом аналогов. Один запрос на выдачу."""
    from .models import AftermarketCatalogPart

    ids = list(part_ids or [])
    if not ids:
        return set()
    return set(
        AftermarketCatalogPart.objects.filter(part_id__in=ids).values_list(
            "part_id", flat=True
        )
    )


def has_physical_stock(part_id) -> bool:
    """Лежит ли деталь физически хоть где-то. Резерв и карантин - тоже остаток."""
    if not part_id:
        return False
    lots = StockLot.objects.filter(
        part_type_id=part_id, status__in=LOT_PHYSICAL_STATUSES, quantity__gt=0
    )
    if lots.exists():
        return True
    return PartItem.objects.filter(
        part_type_id=part_id,
        status__in=ITEM_PHYSICAL_STATUSES,
        current_location__isnull=False,
    ).exists()


def catalog_origin(*, is_aftermarket: bool, has_stock: bool) -> str | None:
    """Подпись происхождения; None - когда сказать нечего и врать не о чем."""
    if not is_aftermarket:
        return None
    return WAREHOUSE if has_stock else AFTERMARKET_CATALOG


def catalog_origin_label(*, is_aftermarket: bool, has_stock: bool) -> str:
    return LABELS.get(catalog_origin(is_aftermarket=is_aftermarket, has_stock=has_stock), "")
