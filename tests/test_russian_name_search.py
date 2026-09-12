"""Поиск по подтверждённому русскому названию детали.

Что здесь гарантируется:

* подтверждённое русское название находится как угодно набранным регистром,
  по началу, по подстроке, по всем словам в любом порядке и с «ё» вместо «е»;
* НЕподтверждённое русское название не находится вообще и клиенту не видно:
  публично разрешено только то, что оператор подтвердил руками;
* артикул по-прежнему сильнее любого совпадения по названию, а точное
  совпадение имени сильнее нестрогого;
* английские названия и приоритет артикулов не сломаны;
* операторский поиск DenisStock и публичный поиск PRO-STOR отвечают одинаково:
  свёртка названия одна на весь проект.

Свёртка регистра сознательно считается в Python, а не через `UPPER()` в базе:
`UPPER` зависит от локали кластера и в локали `C` кириллицу не трогает вовсе.
Отдельный модуль `test_russian_name_search_postgresql.py` проверяет это на
настоящей PostgreSQL, включая базу с локалью `C`.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.actions.models import PartCustomsInfo
from apps.catalog.public_contracts import build_public_part_facts
from apps.catalog.search import search_part_ids
from apps.core.part_lookup import resolve_part_lookup
from apps.core.search_text import fold_search_text
from tests.search_support import Catalog

CONFIRMED = "Прокладка головки блока"


@pytest.fixture
def cat(db):
    return Catalog()


@pytest.fixture
def gasket(cat):
    part = cat.part("GASKET CYLINDER HEAD", article="420931785")
    cat.russian(part, CONFIRMED)
    return part


def _types(hits, part):
    return [hit.match_type for hit in hits if hit.part_id == part.pk]


def _found(query, part):
    return bool(_types(search_part_ids(query), part))


# --- Свёртка -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "folded"),
    [
        ("Прокладка головки блока", "прокладка головки блока"),
        ("ПРОКЛАДКА", "прокладка"),
        ("ПрОкЛаДкА", "прокладка"),
        ("Щётка", "щетка"),
        ("ЩЁТКА", "щетка"),
        ("  много   пробелов  ", "много пробелов"),
        ("GASKET", "gasket"),
        ("", ""),
        (None, ""),
    ],
)
def test_the_one_folding_rule(raw, folded):
    assert fold_search_text(raw) == folded


def test_folding_is_idempotent():
    once = fold_search_text("ЩЁТКА Стеклоочистителя")
    assert fold_search_text(once) == once


# --- Подтверждённое русское название --------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Прокладка головки блока", "exact_name"),
        ("прокладка головки блока", "exact_name"),
        ("ПРОКЛАДКА ГОЛОВКИ БЛОКА", "exact_name"),
        ("прокладка", "name_prefix"),
        ("ПРОКЛАДКА", "name_prefix"),
        ("ПрОкЛаДкА", "name_prefix"),
        ("Прокладка головки", "name_prefix"),
        ("головки блока", "name_partial"),
        ("блока", "name_partial"),
        ("головки прокладка", "name_all_words"),
        ("блока прокладка", "name_all_words"),
    ],
)
def test_a_confirmed_russian_name_is_found(gasket, query, expected):
    assert _types(search_part_ids(query), gasket) == [expected], query


def test_the_letter_yo_and_ye_are_the_same_letter(cat):
    part = cat.part("WIPER BLADE", article="WB-1")
    cat.russian(part, "Щётка стеклоочистителя")

    for query in ("щётка", "щетка", "ЩЕТКА", "Щётка стеклоочистителя", "щетка стеклоочистителя"):
        assert _found(query, part), query


def test_the_same_holds_when_the_operator_typed_ye(cat):
    part = cat.part("WIPER BLADE 2", article="WB-2")
    cat.russian(part, "Щетка стеклоочистителя")

    assert _found("щётка", part)
    assert _found("щетка", part)


def test_a_multiword_query_needs_every_word(cat):
    oil = cat.part("OIL FILTER", article="OF-1")
    cat.russian(oil, "Фильтр масляный")
    air = cat.part("AIR FILTER", article="AF-1")
    cat.russian(air, "Фильтр воздушный")

    assert _found("масляный фильтр", oil)
    assert not _found("масляный фильтр", air), "второе слово обязано совпасть"
    assert _found("фильтр", oil) and _found("фильтр", air)


def test_a_short_word_switches_the_all_words_tier_off(cat):
    part = cat.part("OIL FILTER 2", article="OF-2")
    cat.russian(part, "Фильтр масляный")

    # «на» короче трёх букв: искать по одному «фильтр» и молча выбросить второе
    # слово нельзя - это уже не тот запрос, который набрал человек.
    assert "name_all_words" not in _types(search_part_ids("фильтр на"), part)


# --- Неподтверждённое название --------------------------------------------------------------


@pytest.mark.parametrize(
    "query", ["Прокладка головки блока", "прокладка", "головки блока", "головки прокладка"]
)
def test_an_unconfirmed_russian_name_is_never_found(cat, query):
    part = cat.part("SECRET PART", article="SP-1")
    cat.russian(part, CONFIRMED, confirmed=False)

    assert search_part_ids(query) == []


def test_an_unconfirmed_russian_name_is_never_shown(cat):
    part = cat.part("SECRET PART", article="SP-2")
    cat.russian(part, CONFIRMED, confirmed=False)

    facts = build_public_part_facts([part.pk])[0]

    assert facts.russian_name is None
    assert facts.english_name == "SECRET PART"


def test_confirming_the_name_makes_it_searchable_at_once(cat):
    part = cat.part("LATER CONFIRMED", article="LC-1")
    info = cat.russian(part, "Сальник коленвала", confirmed=False)
    assert not _found("сальник", part)

    info.customs_name_ru_confirmed = True
    info.save(update_fields=["customs_name_ru_confirmed"])

    assert _found("сальник", part)
    assert build_public_part_facts([part.pk])[0].russian_name == "Сальник коленвала"


def test_renaming_keeps_the_search_form_in_step(cat):
    part = cat.part("RENAMED", article="RN-1")
    info = cat.russian(part, "Сальник коленвала")

    info.customs_name_ru = "Подшипник ступицы"
    info.save(update_fields=["customs_name_ru"])

    assert not _found("сальник", part)
    assert _found("подшипник", part)
    assert PartCustomsInfo.objects.get(pk=info.pk).search_name_ru == "подшипник ступицы"


# --- Ранжирование ---------------------------------------------------------------------------


def test_an_article_always_outranks_a_russian_name(cat):
    by_article = cat.part("ARTICLE MATCH", article="ПРОКЛАДКА")
    by_name = cat.part("NAME MATCH", article="NM-1")
    cat.russian(by_name, CONFIRMED)

    hits = search_part_ids("ПРОКЛАДКА")

    assert hits[0].part_id == by_article.pk
    assert hits[0].match_type == "exact_article"
    assert by_name.pk in {hit.part_id for hit in hits}


def test_an_exact_russian_name_outranks_a_prefix_and_a_word_match(cat):
    exact = cat.part("EXACT", article="E-1")
    cat.russian(exact, "Фильтр")
    prefix = cat.part("PREFIX", article="P-1")
    cat.russian(prefix, "Фильтр масляный")
    words = cat.part("WORDS", article="W-1")
    cat.russian(words, "Масляный сменный фильтр")

    hits = search_part_ids("фильтр масляный")
    order = [hit.part_id for hit in hits]

    assert order.index(prefix.pk) < order.index(words.pk)
    assert search_part_ids("фильтр")[0].part_id == exact.pk


def test_english_search_is_unchanged(cat):
    part = cat.part("DRIVE BELT", article="DB-1")
    cat.russian(part, "Ремень вариатора")

    assert _types(search_part_ids("DRIVE BELT"), part) == ["exact_name"]
    assert _types(search_part_ids("drive belt"), part) == ["exact_name"]
    assert _types(search_part_ids("drive"), part) == ["name_prefix"]
    assert _types(search_part_ids("rive be"), part) == ["name_partial"]


def test_article_tiers_are_unchanged(cat):
    exact = cat.part("A", article="AB-1234-CD")
    prefix = cat.part("B", article="AB1234CDX")

    hits = search_part_ids("ab-1234-cd")

    assert hits[0].part_id == exact.pk and hits[0].match_type == "exact_article"
    assert hits[1].part_id == prefix.pk and hits[1].match_type == "article_prefix"


# --- Операторский поиск DenisStock ----------------------------------------------------------


def _operator_ids(query):
    result = resolve_part_lookup(query, allow_partial=True, allow_name=True,
                                 allow_confirmed_ru_name=True)
    return {candidate.part.pk for candidate in result.candidates}


def test_the_operator_search_answers_the_same_russian_queries(gasket):
    for query in ("прокладка", "ПРОКЛАДКА", "ПрОкЛаДкА", "головки блока"):
        assert gasket.pk in _operator_ids(query), query


def test_the_operator_search_also_refuses_an_unconfirmed_name(cat):
    part = cat.part("OPERATOR SECRET", article="OS-1")
    cat.russian(part, "Прокладка клапанной крышки", confirmed=False)

    assert _operator_ids("прокладка") == set()


def test_the_operator_search_folds_yo_like_the_public_one(cat):
    part = cat.part("OPERATOR YO", article="OY-1")
    cat.russian(part, "Щётка стеклоочистителя")

    assert part.pk in _operator_ids("щетка")
    assert part.pk in _operator_ids("щётка")


def test_both_searches_read_the_same_folded_column(gasket):
    stored = PartCustomsInfo.objects.get(part_type=gasket).search_name_ru

    assert stored == fold_search_text(CONFIRMED)
    assert gasket.pk in _operator_ids(stored)
    assert _found(stored, gasket)


# --- Число запросов -------------------------------------------------------------------------


def test_a_multiword_russian_query_costs_a_fixed_number_of_queries(cat):
    """Тир «все слова» это две строки запроса, и он не растёт с числом слов."""
    for index in range(30):
        part = cat.part(f"MULTI {index:03d}", article=f"MW-{index:03d}")
        cat.russian(part, f"Прокладка головки блока {index:03d}")

    counts = []
    for query in ("головки прокладка", "прокладка головки блока 001", "блока головки прокладка"):
        with CaptureQueriesContext(connection) as captured:
            search_part_ids(query)
        counts.append(len(captured.captured_queries))

    assert max(counts) - min(counts) <= 2, counts
    assert max(counts) <= 17, counts


def test_the_number_of_queries_does_not_grow_with_matches(cat):
    counts = []
    # Вторые слова разные: иначе запрос по одному семейству находил бы по
    # опечатке и соседние, и тест мерил бы не то, что заявлено.
    for stem, tail, size in (("Втулка", "передняя", 1), ("Шайба", "медная", 20),
                             ("Кольцо", "стопорное", 50)):
        for index in range(size):
            part = cat.part(f"{stem} EN {index:03d}", article=f"{stem[:2]}-{index:03d}")
            cat.russian(part, f"{stem} {tail} {index:03d}")
        with CaptureQueriesContext(connection) as captured:
            hits = search_part_ids(f"{stem} {tail}")
        assert len(hits) == size, stem
        counts.append(len(captured.captured_queries))

    assert counts[0] == counts[1] == counts[2], counts
