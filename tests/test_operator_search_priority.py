"""Operator-search ranking must favor the strongest useful identity match."""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.actions.models import PartCustomsInfo
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.core.part_lookup import MatchSource, resolve_part_lookup

POSTGRESQL_ONLY = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Cyrillic casefold contract is PostgreSQL-specific"
)


@pytest.fixture
def search_refs(db):
    return Category.objects.create(name="Operator search priority"), Unit.objects.get(name="Штука")


def _part(category, unit, *, article, name):
    part = PartType.objects.create(
        name=name,
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value=article, kind=PartNumber.Kind.OEM)
    return part


def _operator_lookup(query):
    return resolve_part_lookup(
        query,
        allow_partial=True,
        allow_name=True,
        allow_confirmed_ru_name=True,
        partial_exact_numbers_only=True,
    )


def test_exact_english_name_beats_unrelated_article_substring(search_refs):
    category, unit = search_refs
    substring = _part(category, unit, article="PWHEELDISPLAY4", name="DISPLAY")
    exact_name = _part(category, unit, article="123456", name="WHEEL")

    result = _operator_lookup("wheel")

    assert result.found
    assert result.candidate.part.pk == exact_name.pk
    assert result.candidate.part.pk != substring.pk
    assert result.candidate.match_source == MatchSource.NAME


def test_exact_article_remains_above_exact_english_name(search_refs):
    category, unit = search_refs
    exact_article = _part(category, unit, article="WHEEL", name="SOMETHING")
    _part(category, unit, article="123", name="WHEEL")

    result = _operator_lookup("wheel")

    assert result.found
    assert result.candidate.part.pk == exact_article.pk
    assert result.candidate.match_source == MatchSource.EXACT


@pytest.mark.parametrize("query", ("КОЛЕСО", "колесо"))
@POSTGRESQL_ONLY
def test_exact_cyrillic_article_remains_above_confirmed_russian_name(search_refs, query):
    category, unit = search_refs
    exact_article = _part(category, unit, article="КОЛЕСО", name="SOMETHING")
    ru_name = _part(category, unit, article="123", name="WHEEL")
    PartCustomsInfo.objects.create(
        part_type=ru_name,
        customs_name_ru="КОЛЕСО",
        customs_name_ru_confirmed=True,
    )

    result = _operator_lookup(query)

    assert result.found
    assert result.candidate.part.pk == exact_article.pk
    assert result.candidate.match_source == MatchSource.EXACT


def test_exact_english_name_beats_article_prefix(search_refs):
    category, unit = search_refs
    prefix = _part(category, unit, article="WHEEL-ARTICLE", name="DISPLAY")
    exact_name = _part(category, unit, article="123456", name="WHEEL")

    result = _operator_lookup("wheel")

    assert result.found
    assert result.candidate.part.pk == exact_name.pk
    assert result.candidate.part.pk != prefix.pk


def test_multiple_exact_names_remain_a_chooser_not_an_arbitrary_part(search_refs):
    category, unit = search_refs
    first = _part(category, unit, article="WHEEL-1", name="WHEEL")
    second = _part(category, unit, article="WHEEL-2", name="WHEEL")

    result = _operator_lookup("wheel")

    assert result.status == "multiple"
    assert not result.found
    assert {candidate.part.pk for candidate in result.candidates} == {first.pk, second.pk}


@POSTGRESQL_ONLY
def test_multiple_exact_confirmed_russian_names_remain_a_chooser(search_refs):
    category, unit = search_refs
    first = _part(category, unit, article="RU-WHEEL-1", name="FIRST")
    second = _part(category, unit, article="RU-WHEEL-2", name="SECOND")
    for part in (first, second):
        PartCustomsInfo.objects.create(
            part_type=part,
            customs_name_ru="КОЛЕСО",
            customs_name_ru_confirmed=True,
        )

    result = _operator_lookup("колесо")

    assert result.status == "multiple"
    assert not result.found
    assert {candidate.part.pk for candidate in result.candidates} == {first.pk, second.pk}


def test_partial_article_still_works_without_a_stronger_match(search_refs):
    category, unit = search_refs
    part = _part(category, unit, article="PWHEELDISPLAY4", name="DISPLAY")

    result = _operator_lookup("wheel")

    assert result.found
    assert result.candidate.part.pk == part.pk
    assert result.candidate.match_source == MatchSource.NUMBER_PARTIAL


@pytest.mark.parametrize("query", ("КОЛЕСО", "колесо", "Колесо", "кОлЕсО"))
@POSTGRESQL_ONLY
def test_confirmed_exact_russian_name_beats_article_substring(search_refs, query):
    category, unit = search_refs
    substring = _part(category, unit, article="PКОЛЕСОDISPLAY4", name="DISPLAY")
    exact_russian_name = _part(category, unit, article="123456", name="WHEEL")
    PartCustomsInfo.objects.create(
        part_type=exact_russian_name,
        customs_name_ru="КОЛЕСО",
        customs_name_ru_confirmed=True,
    )

    result = _operator_lookup(query)

    assert result.found
    assert result.candidate.part.pk == exact_russian_name.pk
    assert result.candidate.part.pk != substring.pk
    assert result.candidate.match_source == MatchSource.NAME


@pytest.mark.parametrize("query", ("ПРОКЛАДКА", "прокладка", "Прокладка"))
@POSTGRESQL_ONLY
def test_confirmed_russian_name_casefolds_a_second_word(search_refs, query):
    category, unit = search_refs
    part = _part(category, unit, article="GASKET-123", name="GASKET")
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="ПРОКЛАДКА",
        customs_name_ru_confirmed=True,
    )

    result = _operator_lookup(query)

    assert result.found
    assert result.candidate.part.pk == part.pk
    assert result.candidate.match_source == MatchSource.NAME


@POSTGRESQL_ONLY
def test_unconfirmed_russian_name_is_excluded_from_exact_name_tier(search_refs):
    category, unit = search_refs
    part = _part(category, unit, article="UNCONFIRMED-123", name="GASKET")
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="КОЛЕСО",
        customs_name_ru_confirmed=False,
    )

    result = _operator_lookup("колесо")

    assert not result.found
    assert not result.candidates


@POSTGRESQL_ONLY
def test_partial_confirmed_russian_name_still_works(search_refs):
    category, unit = search_refs
    part = _part(category, unit, article="GASKET-123", name="GASKET")
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="ПРОКЛАДКА ДВИГАТЕЛЯ",
        customs_name_ru_confirmed=True,
    )

    result = _operator_lookup("кладка")

    assert result.found
    assert result.candidate.part.pk == part.pk
    assert result.candidate.match_source == MatchSource.NAME


def test_zero_stock_exact_name_remains_discoverable(search_refs):
    category, unit = search_refs
    part = _part(category, unit, article="123456", name="WHEEL")

    result = _operator_lookup("wheel")

    assert result.found
    assert result.candidate.part.pk == part.pk
    assert result.candidate.available == Decimal("0")


def test_exact_name_result_count_does_not_create_n_plus_one_queries(search_refs):
    category, unit = search_refs
    _part(category, unit, article="WHEEL-ONE", name="WHEEL")

    with CaptureQueriesContext(connection) as one:
        assert _operator_lookup("wheel").found

    for index in range(20):
        _part(category, unit, article=f"WHEEL-{index:03d}", name="WHEEL")

    with CaptureQueriesContext(connection) as many:
        result = _operator_lookup("wheel")

    assert result.status == "multiple"
    assert len(many) <= len(one) + 1, (len(one), len(many))


@POSTGRESQL_ONLY
def test_confirmed_russian_name_result_count_does_not_create_n_plus_one_queries(search_refs):
    category, unit = search_refs
    one_part = _part(category, unit, article="RU-ONE", name="ONE")
    PartCustomsInfo.objects.create(
        part_type=one_part,
        customs_name_ru="КОЛЕСО",
        customs_name_ru_confirmed=True,
    )

    with CaptureQueriesContext(connection) as one:
        assert _operator_lookup("колесо").found

    for index in range(20):
        part = _part(category, unit, article=f"RU-{index:03d}", name=f"PART {index}")
        PartCustomsInfo.objects.create(
            part_type=part,
            customs_name_ru="КОЛЕСО",
            customs_name_ru_confirmed=True,
        )

    with CaptureQueriesContext(connection) as many:
        result = _operator_lookup("колесо")

    assert result.status == "multiple"
    assert len(many) <= len(one) + 1, (len(one), len(many))
