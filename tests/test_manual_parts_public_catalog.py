from decimal import Decimal

from apps.brp.models import BrpPartLink
from apps.catalog.models import PartType
from apps.catalog.public_catalog import search_catalog
from apps.catalog.public_contracts import resolve_current_customer_price
from apps.catalog.services import create_manual_part
from apps.catalog_import.models import AftermarketCatalogPart, ArcticCatCatalogPart
from apps.polaris.models import PolarisPartLink


def test_manual_sellable_part_is_public_searchable_and_priced(public_catalog):
    part = create_manual_part(
        name="Топливный фильтр", article="MAN-FILTER-01", price=Decimal("2350")
    )
    public_catalog.stock(part, "3")

    result = search_catalog("топливный фильтр", {})

    assert [card.display_name for card in result.cards] == ["Топливный фильтр"]
    card = result.cards[0]
    assert card.facts.article == "MAN-FILTER-01"
    assert card.facts.available_quantity == Decimal("3")
    assert card.facts.price.price_rub == Decimal("2350.00")
    assert part.is_public is True
    assert part.price_provenance == PartType.PriceProvenance.VALID_MANUAL_EXCEPTION


def test_manual_sellable_part_is_searchable_by_article_without_import_source(public_catalog):
    part = create_manual_part(name="Гильза маслонасоса", article="GL-900", price="1250")

    result = search_catalog("GL-900", {})

    assert [card.display_name for card in result.cards] == ["Гильза маслонасоса"]
    assert not BrpPartLink.objects.filter(part=part).exists()
    assert not PolarisPartLink.objects.filter(part=part).exists()
    assert not AftermarketCatalogPart.objects.filter(part=part).exists()
    assert not ArcticCatCatalogPart.objects.filter(part=part).exists()


def test_manual_part_without_price_is_public_but_never_zero(public_catalog):
    part = create_manual_part(name="Фильтр без цены")

    result = search_catalog("Фильтр без цены", {})

    assert [card.display_name for card in result.cards] == ["Фильтр без цены"]
    assert resolve_current_customer_price(part).status == "clarify"
    assert resolve_current_customer_price(part).price_rub is None


def test_explicitly_hidden_manual_part_stays_private(public_catalog):
    part = create_manual_part(name="Внутренняя заготовка", price="100")
    part.is_public = False
    part.save(update_fields=["is_public"])

    result = search_catalog("Внутренняя заготовка", {})

    assert result.cards == []
