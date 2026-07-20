import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KnowledgeSource:
    source_id: str
    filename: str
    title: str
    keywords: tuple[str, ...]
    route_names: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeChunk:
    source_id: str
    title: str
    text: str
    score: int


HOW_TO = "how_to"
TROUBLESHOOTING = "troubleshooting"
DEFINITION = "definition"
CURRENT_DATA = "current_data"
AMBIGUOUS = "ambiguous"


SOURCES = (
    KnowledgeSource(
        "https-canonical-url",
        "https.md",
        "Ошибка HTTPS и канонический адрес",
        ("err_ssl_protocol_error", "ssl", "https", "сертификат", "голому ip"),
        intents=(TROUBLESHOOTING,),
    ),
    KnowledgeSource(
        "inventory",
        "inventory.md",
        "Инвентаризация и пересчёт ячейки",
        (
            "инвентар",
            "пересч",
            "посчитать ячейк",
            "пересчитать ячейк",
            "фактическ количеств",
            "остаток ещё не измен",
            "почему не совпад",
            "не совпад",
            "остат",
        ),
        route_names=(
            "counting_list",
            "counting_new",
            "counting_detail",
            "counting_convert",
            "inventory_count_list",
            "inventory_count_create",
            "inventory_count_detail",
            "initial_inventory_detail",
        ),
        intents=(HOW_TO, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "receiving",
        "receiving.md",
        "Поступление и приёмка",
        (
            "поступлен",
            "приёмк",
            "приемк",
            "принять",
            "оприход",
            "новую деталь на склад",
            "поставк",
        ),
        route_names=(
            "receipt_list",
            "receipt_create",
            "receipt_detail",
            "scanner_receiving",
            "batch_list",
            "batch_detail",
        ),
        intents=(HOW_TO, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "movements",
        "movements.md",
        "Перемещение",
        ("перемест", "перенест", "другую ячейк", "сменить ячейк"),
        route_names=("scanner_move", "item_move", "lot_move"),
        intents=(HOW_TO, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "sales-reservations",
        "sales-and-reservations.md",
        "Продажи, быстрые действия и резервы",
        (
            "продаж",
            "продать",
            "отменить ошибочную продаж",
            "ошибочн продаж",
            "резерв",
            "бронь",
            "заброниров",
            "быстр действия",
        ),
        route_names=(
            "sale_list",
            "sale_detail",
            "reservation_list",
            "reservation_detail",
            "actions_scan",
            "actions_report",
            "actions_cancel",
        ),
        intents=(HOW_TO, TROUBLESHOOTING, DEFINITION),
    ),
    KnowledgeSource(
        "scanner",
        "scanner.md",
        "Сканеры и штрихкоды",
        (
            "скан",
            "штрихкод",
            "найденную деталь",
            "найденной детали",
            "нераспознан",
            "добавить найден",
        ),
        route_names=("scanner", "scanner_receiving", "scanner_move", "unresolved_list"),
        intents=(HOW_TO, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "search-parts",
        "search-and-parts.md",
        "Поиск и карточка детали",
        (
            "найти",
            "поиск",
            "где лежит",
            "в какой ячейк",
            "карточк детал",
            "точный артикул",
            "номер детал",
        ),
        route_names=("part_search", "scanner", "part_list", "part_detail"),
        intents=(HOW_TO, CURRENT_DATA, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "stock-locations",
        "stock-and-locations.md",
        "Остатки, ячейки и история",
        (
            "остат",
            "баланс",
            "ячейк",
            "структур склада",
            "истори движен",
            "журнал движен",
        ),
        route_names=(
            "balance_list",
            "warehouse_index",
            "location_detail",
            "item_detail",
            "lot_detail",
            "movement_list",
            "movement_detail",
        ),
        intents=(HOW_TO, CURRENT_DATA, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "returns-repairs-writeoffs",
        "returns-repairs-writeoffs.md",
        "Ремонты, возвраты и списания",
        ("ремонт", "возврат", "вернуть", "списан", "брак", "утилиз"),
        route_names=(
            "repair_order_list",
            "repair_order_detail",
            "return_list",
            "return_detail",
            "write_off_list",
            "write_off_detail",
        ),
        intents=(HOW_TO, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "reports",
        "reports.md",
        "Отчёты, история и экспорт",
        (
            "отчёт",
            "отчет",
            "экспорт",
            "excel",
            "csv",
            "тамож",
            "статист",
            "история действий",
            "журнал действий",
        ),
        route_names=("reports_dashboard", "statistics_dashboard", "actions_report"),
        intents=(HOW_TO, CURRENT_DATA, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "pricing",
        "pricing.md",
        "Цены, каталоги и импорт",
        (
            "цен",
            "клиентск",
            "рекомендован",
            "курс",
            "нацен",
            "brp",
            "polaris",
            "импорт прайс",
        ),
        route_names=("price_settings", "brp_search", "polaris_search"),
        intents=(HOW_TO, CURRENT_DATA, DEFINITION),
    ),
    KnowledgeSource(
        "permissions",
        "permissions.md",
        "Роли и права",
        ("прав", "роль", "доступ", "403", "пользовател", "не вижу раздел"),
        route_names=("user_list", "user_create", "user_edit"),
        intents=(HOW_TO, TROUBLESHOOTING, DEFINITION),
    ),
    KnowledgeSource(
        "troubleshooting",
        "troubleshooting.md",
        "Типичные проблемы оператора",
        (
            "почему",
            "не работает",
            "ошиб",
            "не измен",
            "не совпад",
            "не появ",
            "не выполня",
            "недоступ",
        ),
        intents=(TROUBLESHOOTING,),
    ),
    KnowledgeSource(
        "navigation",
        "navigation.md",
        "Навигация",
        ("где раздел", "в меню", "пункт меню", "навигац", "вкладк", "sidebar"),
        intents=(HOW_TO, TROUBLESHOOTING),
    ),
    KnowledgeSource(
        "glossary",
        "glossary.md",
        "Термины DenisStock",
        ("что означает", "что такое", "определение", "значит", "термин"),
        intents=(DEFINITION,),
    ),
    KnowledgeSource(
        "overview",
        "overview.md",
        "DenisStock: краткий контекст",
        (),
    ),
)

_TOKEN_RE = re.compile(r"[0-9a-zа-яё_]+", re.IGNORECASE)
_ROOT = Path(__file__).resolve().parents[3] / "docs" / "ai-support"
_OVERVIEW_ID = "overview"
_HOW_TO_MARKERS = ("как ", "как мне", "объясни", "пошаг", "что нужно сделать")
_TROUBLESHOOTING_MARKERS = (
    "почему",
    "не работает",
    "ошиб",
    "не измен",
    "не совпад",
    "не появ",
    "не могу",
    "не получается",
)
_DEFINITION_MARKERS = ("что означает", "что такое", "что значит", "зачем нужен")
_CURRENT_DATA_MARKERS = (
    "сколько сейчас",
    "какой сейчас",
    "какая сейчас",
    "где сейчас",
    "текущий остат",
    "текущая цен",
    "текущий статус",
)


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(value)}


def detect_intent(query: str) -> str:
    lowered = " ".join((query or "").lower().split())
    if any(marker in lowered for marker in _DEFINITION_MARKERS):
        return DEFINITION
    if any(marker in lowered for marker in _TROUBLESHOOTING_MARKERS):
        return TROUBLESHOOTING
    if any(marker in lowered for marker in _CURRENT_DATA_MARKERS):
        return CURRENT_DATA
    if any(marker in lowered for marker in _HOW_TO_MARKERS):
        return HOW_TO
    if len(_tokens(lowered)) < 2:
        return AMBIGUOUS
    return AMBIGUOUS


def _read_source(source: KnowledgeSource) -> str:
    root = _ROOT.resolve()
    path = (root / source.filename).resolve()
    if path.parent != root:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _score(
    source: KnowledgeSource,
    query: str,
    query_tokens: set[str],
    *,
    intent: str,
    route_name: str,
) -> int:
    if source.source_id == _OVERVIEW_ID:
        return 0
    lowered = query.lower()
    source_tokens = _tokens(f"{source.title} {' '.join(source.keywords)}")
    score = len(query_tokens & source_tokens)
    score += sum(8 for keyword in source.keywords if keyword in lowered)
    if route_name and route_name in source.route_names:
        score += 24
    if score > 0 and intent in source.intents:
        score += 2
    if source.source_id == "https-canonical-url" and "err_ssl_protocol_error" in lowered:
        score += 50
    return score


def retrieve(
    query: str,
    *,
    route_context: dict[str, str] | None = None,
    limit: int = 4,
    max_chars: int = 6000,
) -> tuple[KnowledgeChunk, ...]:
    """Return deterministic topic context plus a compact DenisStock overview."""
    intent = detect_intent(query)
    query_tokens = _tokens(query)
    route_name = (route_context or {}).get("route_name", "")
    ranked = []
    overview = None
    for source in SOURCES:
        text = _read_source(source)
        if not text:
            continue
        chunk = KnowledgeChunk(
            source_id=source.source_id,
            title=source.title,
            text=text,
            score=_score(
                source,
                query,
                query_tokens,
                intent=intent,
                route_name=route_name,
            ),
        )
        if source.source_id == _OVERVIEW_ID:
            overview = chunk
        else:
            ranked.append(chunk)
    ranked.sort(key=lambda item: (-item.score, item.source_id))

    selected = [chunk for chunk in ranked if chunk.score > 0][: max(limit - 1, 0)]
    if not selected and overview is not None:
        selected.append(overview)
    elif overview is not None and len(selected) < limit:
        selected.append(overview)

    bounded = []
    used = 0
    for chunk in selected:
        remaining = max_chars - used
        if remaining <= 0:
            break
        text = chunk.text[:remaining]
        bounded.append(KnowledgeChunk(chunk.source_id, chunk.title, text, chunk.score))
        used += len(text)
    return tuple(bounded)
