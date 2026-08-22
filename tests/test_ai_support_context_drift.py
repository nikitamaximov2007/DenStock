"""База знаний поддержки должна описывать сегодняшний продукт.

Встроенная поддержка отвечает по документам из `docs/ai-support`. Документы
живут отдельно от кода, поэтому расходятся с ним молча: экран меняется, а
поддержка продолжает объяснять прежний порядок. Человек при этом не понимает,
что его обманули, - он видит уверенный ответ и идёт искать несуществующую
кнопку.

Здесь закреплены те места, где расхождение уже случалось или обошлось бы
дороже всего: заведение детали вручную, смысл денежных величин в отчётах и
названия полей.

Проверяется не текст ради текста, а совпадение с настоящим продуктом: подписи
берутся из самих форм, маршруты - из адресов приложения.
"""
from pathlib import Path

import pytest
from django.urls import NoReverseMatch, reverse

from apps.ai_support.knowledge.index import SOURCES, retrieve
from apps.catalog.forms import ManualPartForm

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "docs" / "ai-support"


def text_of(name: str) -> str:
    return (KNOWLEDGE / name).read_text(encoding="utf-8")


# --- Вопросы, которые оператор задаёт своими словами -----------------------------


NEW_QUESTIONS = (
    ("Как добавить деталь, которой нет в каталоге?", "search-parts"),
    ("Нужно завести новую деталь, что делать?", "search-parts"),
    ("Не нашёл деталь в базе, как её создать?", "search-parts"),
    ("Сколько нам стоили детали, выданные клиенту в ремонт?", "reports"),
    ("Где посмотреть себестоимость деталей в ремонте?", "reports"),
    ("Как узнать клиентскую цену детали?", "pricing"),
    # Найдено этой же проверкой: признак был записан формой слова «списан»,
    # а не основой, и вопрос уходил в приёмку.
    ("Как списать сломанную деталь?", "returns-repairs-writeoffs"),
)


@pytest.mark.parametrize(("question", "expected"), NEW_QUESTIONS)
def test_the_question_reaches_the_right_topic(question, expected):
    chunks = retrieve(question)
    assert chunks, f"на вопрос «{question}» поддержке нечего показать"
    assert chunks[0].source_id == expected, (
        f"«{question}» ведёт в «{chunks[0].source_id}», а нужно «{expected}»"
    )


@pytest.mark.parametrize(("question", "_expected"), NEW_QUESTIONS)
def test_the_answer_is_the_same_every_time(question, _expected):
    assert retrieve(question) == retrieve(question)


# --- Заведение детали ------------------------------------------------------------


def test_the_knowledge_describes_the_form_that_exists_now():
    """Раньше поддержке было нечего ответить: раздела про это не было вовсе."""
    body = text_of("search-and-parts.md")
    assert "Добавить деталь" in body
    for label in (field.label for field in ManualPartForm().visible_fields()):
        assert label in body, f"поддержка не знает поле «{label}»"


def test_the_knowledge_does_not_teach_the_old_impossible_order():
    """Прежний порядок требовал сначала завести категорию. Так больше нельзя."""
    body = text_of("search-and-parts.md")
    assert "сначала создайте категорию" not in body.lower()
    assert "Добавлено вручную" in body, "не сказано, что категория подставляется сама"


def test_the_knowledge_still_says_that_a_card_is_not_stock():
    body = text_of("search-and-parts.md")
    assert "не создаёт" in body and "остат" in body


def test_the_knowledge_explains_that_a_matching_number_is_not_a_refusal():
    body = text_of("search-and-parts.md")
    assert "Всё равно создать" in body


# --- Деньги в отчётах ------------------------------------------------------------


def test_the_knowledge_states_that_repair_cost_is_not_revenue():
    """Самое дорогое расхождение: поддержка могла бы назвать расход выручкой."""
    body = text_of("reports.md")
    assert "не является выручкой" in body or "не выручка" in body


def test_the_knowledge_forbids_adding_the_two_amounts():
    body = text_of("reports.md")
    assert "складывать" in body.lower()


def test_the_knowledge_names_both_columns_as_the_screen_does():
    body = text_of("reports.md")
    assert "Сумма (₽)" in body
    assert "Себестоимость (₽)" in body


def test_the_knowledge_mentions_that_the_amounts_are_historical():
    body = text_of("reports.md")
    assert "историчн" in body or "зафиксирован" in body


def test_the_knowledge_mentions_the_permission_on_money():
    body = text_of("reports.md")
    assert "прав" in body.lower() and "закупочн" in body.lower()


# --- Название цены ---------------------------------------------------------------


def test_the_knowledge_uses_the_same_name_for_the_catalog_price():
    body = text_of("pricing.md")
    assert "Рекомендуемая цена" in body
    assert "Цена продажи за ед. (₽)" in body, "цена строки продажи должна остаться отдельной"


# --- Общая защита от расхождения --------------------------------------------------


def test_every_route_the_knowledge_points_at_still_exists():
    """Исчезнувший маршрут означает, что раздел больше никогда не сработает.

    Поддержка подбирает раздел в том числе по тому, на какой странице сейчас
    находится человек. Если маршрут переименовали, подсказка молча перестаёт
    появляться именно там, где она нужнее всего.
    """
    missing = []
    for source in SOURCES:
        for name in source.route_names:
            try:
                reverse(name)
            except NoReverseMatch:
                try:
                    reverse(name, args=[1])
                except NoReverseMatch:
                    missing.append(f"{source.source_id} -> {name}")
    assert not missing, f"поддержка ссылается на несуществующие адреса: {missing}"


def test_every_source_file_the_index_names_is_present():
    missing = [s.filename for s in SOURCES if not (KNOWLEDGE / s.filename).is_file()]
    assert not missing, f"в docs/ai-support нет файлов: {missing}"
