"""Validity gate for the disposable Stage 2 qualification corpus.

This intentionally calls the shared Search 2.0 service.  A raw row count is
not qualification evidence unless these canonical identities resolve first.
"""

import pytest
from django.core.management import call_command

from apps.catalog.models import PartNumber, PartType
from apps.catalog.search import search_part_ids, supports_trigram


@pytest.fixture
def qualification_corpus(db):
    call_command(
        "generate_public_catalog_stage2_qualification",
        "--confirm-isolated",
        "--size=20",
        "--batch-size=7",
    )
    return {
        number.value: number.part_id
        for number in PartNumber.objects.filter(kind=PartNumber.Kind.ARTICLE)
    }


def _first(query):
    return search_part_ids(query)[0]


def test_qualification_generator_requires_explicit_isolated_confirmation(db):
    with pytest.raises(Exception, match="confirm-isolated"):
        call_command("generate_public_catalog_stage2_qualification", "--size=20")
    assert not PartType.objects.exists()


def test_miniature_qualification_corpus_exercises_canonical_search_paths(qualification_corpus):
    ids = qualification_corpus
    assert PartType.objects.count() == 20
    assert PartNumber.objects.count() == 20

    assert _first("420-892-388").part_id == ids["420-892-388"]
    assert _first("420-892-388").match_type == "exact_article"
    assert _first("420892388").part_id == ids["420-892-388"]
    assert _first("420892388").match_type == "normalized_exact_article"
    assert _first("4208").part_id == ids["420-892-388"]
    assert _first("8923").part_id == ids["420-892-388"]
    assert _first("BEARING").part_id == ids["Q-BRG-01"]
    assert _first("BEARING").match_type == "exact_name"
    assert _first("ПРОКЛАДКА").part_id == ids["Q-GSK-01"]
    assert _first("ПРОКЛАДКА").match_type == "exact_name"

    # The duplicate text is on a separate unconfirmed row.  It is absent even
    # though the confirmed counterpart is returned for the same query.
    assert ids["Q-RU-UNC-01"] not in [hit.part_id for hit in search_part_ids("ПРОКЛАДКА")]
    assert _first("BEARNG-01").part_id == ids["BEARNG-01"]
    assert _first("BEARNG-01").match_type == "exact_article"


@pytest.mark.postgresql
def test_miniature_qualification_corpus_exercises_typo_paths(qualification_corpus):
    if not supports_trigram():
        pytest.skip("Needs PostgreSQL 16 with pg_trgm")
    ids = qualification_corpus
    bearing = next(hit for hit in search_part_ids("bearng") if hit.part_id == ids["Q-BRG-01"])
    gasket = next(hit for hit in search_part_ids("проклатка") if hit.part_id == ids["Q-GSK-01"])
    assert bearing.match_type == "name_fuzzy"
    assert gasket.match_type == "name_fuzzy"
