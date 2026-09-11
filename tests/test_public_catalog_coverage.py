"""Read-only public catalog coverage report."""

import json
from io import StringIO

from django.core.management import call_command

from apps.catalog.public_coverage import collect_coverage
from apps.catalog.public_photos import publish_photo
from tests.public_catalog_support import assert_no_writes, capture


def _seed(public_catalog):
    stocked = public_catalog.part(
        "Stocked RU", article="COV-1", maker="WISECO", russian="Поршень", application="ГИДРОЦИКЛ"
    )
    public_catalog.stock(stocked, "2")
    photo_part = public_catalog.part("With photo", article="COV-2", maker="WISECO", price=None)
    publish_photo(public_catalog.image(photo_part), source="own", by=public_catalog.user)
    public_catalog.image(public_catalog.part("Candidate photo only", article="COV-3"))
    analog = public_catalog.part("Analog", article="COV-4", maker="PROX")
    public_catalog.analog(stocked, analog)
    guessed = public_catalog.part(
        "Guessed", maker=None, russian="Черновик", russian_confirmed=False
    )
    public_catalog.analog(stocked, guessed, confirmed=False)
    public_catalog.part("Hidden", article="COV-6", public=False)
    public_catalog.part("Retired", article="COV-7", active=False)


def test_coverage_counts_follow_the_public_rules(public_catalog):
    _seed(public_catalog)

    with capture() as queries:
        report = collect_coverage()

    assert_no_writes(queries)
    assert report.total_parts == 7
    assert report.public_parts == 5
    assert (report.hidden_parts, report.retired_parts) == (1, 1)
    assert (report.with_confirmed_ru, report.without_confirmed_ru) == (1, 4)
    assert (report.with_published_photo, report.without_photo) == (1, 4)
    assert (report.with_confirmed_analogs, report.confirmed_originals) == (2, 1)
    assert report.confirmed_analogs == 1
    assert (report.in_stock, report.zero_stock) == (1, 4)
    assert (report.price_known, report.price_unknown) == (4, 1)
    assert (report.with_article, report.without_article) == (4, 1)
    assert (report.with_manufacturer, report.without_manufacturer) == (4, 1)
    assert dict(report.top_manufacturers)["WISECO"] == 2
    assert report.applications["ГИДРОЦИКЛ"] == 1 and report.without_application == 4


def test_command_prints_text_and_json(public_catalog):
    _seed(public_catalog)
    text, raw = StringIO(), StringIO()

    call_command("public_catalog_coverage_report", stdout=text)
    call_command("public_catalog_coverage_report", "--json", stdout=raw)

    assert "Публичных (видны покупателю)" in text.getvalue()
    assert "—" not in text.getvalue()
    assert json.loads(raw.getvalue())["public_parts"] == 5
