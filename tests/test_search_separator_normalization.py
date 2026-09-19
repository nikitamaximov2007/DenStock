"""Разделители в запросе не меняют результат: «O-RING» = «O RING» = «ORING»."""
import pytest

from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.catalog.search import search_part_ids
from apps.core.search_text import compact_search_text


@pytest.fixture
def catalog(db):
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    brand, _ = Manufacturer.objects.get_or_create(name="BRP")

    def make(name, article=""):
        part = PartType.objects.create(
            name=name, category=category, unit=unit, manufacturer=brand
        )
        if article:
            PartNumber.objects.create(
                part=part, value=article, kind=PartNumber.Kind.ARTICLE
            )
        return part

    return {
        "hyphen": make("O-RING", "420-931-410"),
        "spaced": make("O RING SEAL"),
        "joined": make("ORINGX"),
        "other": make("BEARING DRIVE", "111-222-333"),
    }


def ids(query):
    return [hit.part_id for hit in search_part_ids(query)]


def kinds(query):
    return {hit.part_id: hit.match_type for hit in search_part_ids(query)}


@pytest.mark.parametrize("query", ["o-ring", "o ring", "oring", "O-RING", "  O   Ring "])
def test_every_spelling_of_the_same_name_finds_the_same_part(catalog, query):
    assert catalog["hyphen"].pk in ids(query)


@pytest.mark.parametrize("query", ["420-931-410", "420 931 410", "420931410"])
def test_every_spelling_of_the_same_article_finds_the_same_part(catalog, query):
    assert catalog["hyphen"].pk in ids(query)


def test_the_name_as_written_still_outranks_the_glued_match(catalog):
    hits = {hit.part_id: hit.rank for hit in search_part_ids("O-RING")}
    assert hits[catalog["hyphen"].pk] < hits.get(catalog["spaced"].pk, 99)


def test_an_exact_original_name_keeps_its_own_tier(catalog):
    assert kinds("O-RING")[catalog["hyphen"].pk] == "exact_name"


def test_a_separator_only_difference_is_its_own_tier(catalog):
    assert kinds("o ring")[catalog["hyphen"].pk] == "normalized_exact_name"


def test_normalization_does_not_merge_unrelated_parts(catalog):
    assert catalog["other"].pk not in ids("o ring")


def test_the_stored_name_is_never_rewritten(catalog):
    catalog["hyphen"].refresh_from_db()
    assert catalog["hyphen"].name == "O-RING"
    assert catalog["hyphen"].search_name_compact == "oring"


def test_a_decimal_point_is_not_a_separator():
    assert compact_search_text("1.5 мм") == "1.5мм"
