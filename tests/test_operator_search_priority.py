"""Operator-search ranking must favor the strongest useful identity match."""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.actions.models import PartCustomsInfo
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.core.part_lookup import MatchSource, resolve_part_lookup


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


def test_partial_article_still_works_without_a_stronger_match(search_refs):
    category, unit = search_refs
    part = _part(category, unit, article="PWHEELDISPLAY4", name="DISPLAY")

    result = _operator_lookup("wheel")

    assert result.found
    assert result.candidate.part.pk == part.pk
    assert result.candidate.match_source == MatchSource.NUMBER_PARTIAL


def test_confirmed_exact_russian_name_beats_article_substring(search_refs):
    category, unit = search_refs
    substring = _part(category, unit, article="PKOLESODISPLAY4", name="DISPLAY")
    exact_russian_name = _part(category, unit, article="123456", name="WHEEL")
    PartCustomsInfo.objects.create(
        part_type=exact_russian_name,
        customs_name_ru="КОЛЕСО",
        customs_name_ru_confirmed=True,
    )

    result = _operator_lookup("КОЛЕСО")

    assert result.found
    assert result.candidate.part.pk == exact_russian_name.pk
    assert result.candidate.part.pk != substring.pk
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
