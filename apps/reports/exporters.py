"""Слой 22 — экспорт отчётов в CSV. Чистое форматирование, БЕЗ бизнес-логики.

Функции принимают уже посчитанные dataclass-отчёты из `apps.reports.services`
(те же, что показывает UI) и флаг `include_costs`, возвращают `(header, rows)` —
формат-агностично (будущий XLSX переиспользует эти же строки). Бизнес-логику и ORM
здесь не трогаем: цифры в файле = цифры в интерфейсе.

CSV под Excel (RU): UTF-8 с BOM, разделитель `;`, Decimal строкой с запятой
(`900,00`). Финансовые колонки при `include_costs=False` физически НЕ пишутся.
"""
import csv
from decimal import ROUND_HALF_UP, Decimal

from django.http import HttpResponse
from django.utils import timezone

DELIM = ";"
BOM = "﻿"  # чтобы Excel (Windows/RU) распознал UTF-8 и кириллицу


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _money(value) -> str:
    return format(_decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP), "f")


def _quantity(value) -> str:
    decimal = _decimal(value)
    if decimal == decimal.to_integral_value():
        return format(decimal.quantize(Decimal("1")), "f")
    return format(decimal.normalize(), "f").replace(".", ",")


def _d(value) -> str:
    return f"{value:%Y-%m-%d}"


def export_filename(slug: str, period=None) -> str:
    """ASCII-имя файла: denstock-<отчёт>-<даты>.csv."""
    if period is not None:
        return f"denstock-{slug}-{_d(period.date_from)}-{_d(period.date_to)}.csv"
    return f"denstock-{slug}-{_d(timezone.localdate())}.csv"


def csv_response(filename: str, header: list, rows: list) -> HttpResponse:
    """Собрать CSV-ответ: BOM + `;`-разделитель + attachment-имя. Файлы не храним."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write(BOM)
    writer = csv.writer(response, delimiter=DELIM, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return response


# --- Период-отчёты (одна строка KPI) -----------------------------------------


def sales_rows(report, period, *, include_costs):
    header = ["Период с", "Период по", "Продаж", "Строк продаж"]
    row = [_d(period.date_from), _d(period.date_to), report.count, report.line_count]
    if include_costs:
        header += ["Выручка (₽)", "Себестоимость (₽)", "Валовая прибыль (₽)"]
        row += [_money(report.revenue), _money(report.cost), _money(report.profit)]
    return header, [row]


def returns_rows(report, period, *, include_costs):
    header = ["Период с", "Период по", "Документов возврата", "Возвращено (кол-во)"]
    row = [_d(period.date_from), _d(period.date_to), report.count, _quantity(report.quantity)]
    if include_costs:
        header += ["Себестоимость возвращённого (₽)"]
        row += [_money(report.cost)]
    return header, [row]


def repairs_rows(report, period, *, include_costs):
    header = ["Период с", "Период по", "Заказов"]
    row = [_d(period.date_from), _d(period.date_to), report.count]
    if include_costs:
        header += ["Себестоимость выданного (₽)"]
        row += [_money(report.issued_cost)]
    return header, [row]


def stocktaking_rows(report, period, *, include_costs):
    header = [
        "Период с", "Период по", "Инвентаризаций",
        "Оприходовано (кол-во)", "Списано сверкой (кол-во)",
    ]
    row = [
        _d(period.date_from), _d(period.date_to), report.count,
        _quantity(report.adjust_in_qty), _quantity(report.adjust_out_qty),
    ]
    if include_costs:
        header += ["Оприходовано (₽)", "Списано сверкой (₽)"]
        row += [_money(report.adjust_in_cost), _money(report.adjust_out_cost)]
    return header, [row]


# --- Списания (таблица по причине) -------------------------------------------


def writeoffs_rows(report, period, *, include_costs):
    header = ["Причина", "Документов"]
    if include_costs:
        header += ["Себестоимость (₽)"]
    rows = []
    for r in report.by_reason:
        row = [r.reason, r.count]
        if include_costs:
            row += [_money(r.cost)]
        rows.append(row)
    return header, rows


# --- Остатки / низкие остатки (без денежных полей) ---------------------------


def stock_rows(report):
    header = ["Ячейка", "Доступно", "Зарезервировано", "Карантин"]
    rows = [
        [r.location, _quantity(r.available), _quantity(r.reserved), _quantity(r.quarantine)]
        for r in report.by_location
    ]
    rows.append([
        "ИТОГО", _quantity(report.total_available),
        _quantity(report.total_reserved), _quantity(report.total_quarantine),
    ])
    return header, rows


def low_stock_rows(rows):
    header = ["Название детали", "Артикул", "Производитель", "Доступно", "Минимум"]
    out = [
        [
            r.part_type,
            r.exact_number,
            r.manufacturer,
            _quantity(r.available),
            _quantity(r.min_stock_level),
        ]
        for r in rows
    ]
    return header, out
