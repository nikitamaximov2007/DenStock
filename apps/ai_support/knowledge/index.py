import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KnowledgeSource:
    source_id: str
    filename: str
    title: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeChunk:
    source_id: str
    title: str
    text: str
    score: int


SOURCES = (
    KnowledgeSource(
        "https-canonical-url",
        "https.md",
        "Ошибка HTTPS и канонический адрес",
        ("ssl", "err_ssl_protocol_error", "https", "сертификат", "ip", "продаж"),
    ),
    KnowledgeSource(
        "sales-safe-check",
        "sales.md",
        "Безопасная проверка продажи",
        ("продаж", "провест", "дубл", "остат", "отмен"),
    ),
    KnowledgeSource(
        "receiving",
        "receiving.md",
        "Поступление",
        ("поступлен", "принят", "приёмк", "детал", "поставк"),
    ),
    KnowledgeSource(
        "inventory",
        "inventory.md",
        "Остатки и инвентаризация",
        ("остат", "инвентар", "ячейк", "движен", "не совпад"),
    ),
    KnowledgeSource(
        "navigation",
        "navigation.md",
        "Навигация DenisStock",
        ("где", "раздел", "истори", "поиск", "склад", "ремонт", "резерв"),
    ),
)

_TOKEN_RE = re.compile(r"[0-9a-zа-яё_]+", re.IGNORECASE)
_ROOT = Path(__file__).resolve().parent.parent / "knowledge_pack"


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(value)}


def _score(source: KnowledgeSource, query: str, query_tokens: set[str], text: str) -> int:
    haystack_tokens = _tokens(f"{source.title} {' '.join(source.keywords)} {text}")
    score = len(query_tokens & haystack_tokens)
    lowered = query.lower()
    score += sum(4 for keyword in source.keywords if keyword in lowered)
    if source.source_id == "https-canonical-url" and "err_ssl_protocol_error" in lowered:
        score += 30
    return score


def retrieve(query: str, *, limit: int = 4, max_chars: int = 6000) -> tuple[KnowledgeChunk, ...]:
    query_tokens = _tokens(query)
    ranked = []
    for source in SOURCES:
        path = (_ROOT / source.filename).resolve()
        if path.parent != _ROOT.resolve():
            continue
        text = path.read_text(encoding="utf-8").strip()
        ranked.append(
            KnowledgeChunk(
                source_id=source.source_id,
                title=source.title,
                text=text,
                score=_score(source, query, query_tokens, text),
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.source_id))
    selected = []
    used = 0
    for chunk in ranked:
        if chunk.score <= 0 or len(selected) >= limit:
            break
        remaining = max_chars - used
        if remaining <= 0:
            break
        text = chunk.text[:remaining]
        selected.append(KnowledgeChunk(chunk.source_id, chunk.title, text, chunk.score))
        used += len(text)
    return tuple(selected)
