"""Таможенная форма из зафиксированных данных заказа."""

from io import BytesIO

from apps.actions.services import (
    ORDERED_PROVENANCE,
    SALES_REPAIRS_PROVENANCE,
    export_customs_xlsx,
)


def export_customs_order_xlsx(order) -> BytesIO:
    """Два листа одного шаблона, без повторного обращения к каталогам.

    Каждая сохранённая исходная строка остаётся отдельной: продажа и
    заказанная деталь одного артикула не теряют своё происхождение.
    """
    originals, analogs = [], []
    for line in order.lines.all():
        row = {
            "number": line.article,
            "name_ru": line.name_ru,
            "name_en": line.name_en,
            "manufacturer": line.manufacturer,
            "country": line.country,
            "gross_weight_kg": line.gross_weight_kg,
            "net_weight_kg": line.net_weight_kg,
            "quantity": line.quantity,
            "usd_price": line.wholesale_usd,
            "application_area": line.application_area,
            "provenance": ORDERED_PROVENANCE if line.is_ordered else SALES_REPAIRS_PROVENANCE,
        }
        (analogs if line.is_analog else originals).append(row)
    return export_customs_xlsx(sheet_rows=[("Оригиналы", originals), ("Аналоги", analogs)])
