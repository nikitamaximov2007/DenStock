"""Выбор существующей карточки детали по части названия (приёмка и пересчёт).

Сканер по-прежнему работает только с точными идентификаторами. Здесь отдельная,
дополнительная возможность: сотрудник набирает «сальник», видит подходящие
карточки и выбирает одну. Операция дальше идёт по её ID, а не по строке
запроса: «сальник» не идентифицирует складскую позицию.

Поиск переиспользует ранжированный движок каталога (apps.catalog.search):
каждый тир - отдельный индексированный запрос с LIMIT, каталог в Python не
загружается, регистр кириллицы сворачивается в Python, а не через UPPER()
базы. Возвращаются только активные карточки PartType: внешние справочники
BRP/Polaris, экземпляры и партии сюда не попадают.
"""

from __future__ import annotations

import re

from apps.catalog.models import PartType
from apps.catalog.search import clean_query, search_part_ids
from apps.inventory.presentation import part_exact_number

PICKER_LIMIT = 20
# Движок ранжирует с запасом, архивные карточки отбрасываются после.
_SEARCH_WINDOW = 60
MIN_PICKER_QUERY = 2

# Настоящие штрихкоды и артикулы не содержат кириллицы, пробелов и «*».
# Строка с ними, которая не распознана как код, - это набранное название.
_NAME_MARKERS = re.compile(r"[\s*]")


def looks_like_name_query(raw: str | None) -> bool:
    value = (raw or "").strip()
    if not value:
        return False
    # Exact identifier resolution always runs before this heuristic. For
    # unrecognized input, accept ordinary one-word Latin and Cyrillic names
    # as well as spaced/compact dimensional names. Punctuation and digits
    # keep code-like values (for example ``MУСОР-XYZ``) on the unknown-code
    # path unless the operator explicitly searches in the live picker.
    if _NAME_MARKERS.search(value):
        return True
    return value.isalpha()


def pickable_parts():
    return PartType.objects.filter(is_active=True)


def get_pickable_part(part_id) -> PartType | None:
    """Карточку, выбранную на экране, проверяем заново на сервере."""
    try:
        pk = int(part_id)
    except (TypeError, ValueError):
        return None
    if pk <= 0:
        return None
    return (
        pickable_parts()
        .select_related("manufacturer", "brp_link__brp_part", "polaris_link__polaris_part")
        .filter(pk=pk)
        .first()
    )


def search_pickable_parts(raw: str | None, *, limit: int = PICKER_LIMIT) -> list[dict]:
    """До ``limit`` активных карточек в порядке ранжирования движка каталога.

    Только то, что нужно, чтобы различить похожие позиции: ID, название,
    артикул и производитель. Себестоимость и остатки не возвращаются.
    """
    query = clean_query(raw)
    if len(query) < MIN_PICKER_QUERY:
        return []
    hits = search_part_ids(query, limit=_SEARCH_WINDOW, active_only=True)
    ids = [hit.part_id for hit in hits]
    parts = {
        part.pk: part
        for part in pickable_parts()
        .filter(pk__in=ids)
        .select_related("manufacturer", "brp_link__brp_part", "polaris_link__polaris_part")
    }
    rows = []
    for part_id in ids:
        part = parts.get(part_id)
        if part is None:
            continue
        rows.append(
            {
                "id": part.pk,
                "name": part.name,
                "article": part_exact_number(part, default=""),
                "manufacturer": part.manufacturer.name if part.manufacturer_id else "",
            }
        )
        if len(rows) >= limit:
            break
    return rows
