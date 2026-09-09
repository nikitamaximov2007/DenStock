"""Независимая проверка Stage 7-9 на кандидате e4105a4 (ревью-сессия).

Stage 7, 8 и 9 реализованы и запушены. Эти проверки написаны отдельной
сессией и не повторяют тесты реализации: они закрывают то, чего в них нет.
Продуктовая логика не меняется.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.warehouse.addresses import (
    normalize_address_input,
    short_address,
)
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def boss(db, django_user_model):
    user = django_user_model.objects.create_superuser(username="boss", password=PASSWORD)
    return user


# --- Stage 8: одна и та же ячейка, обе формы адреса ------------------------------------


@pytest.mark.django_db
def test_legacy_and_short_input_resolve_to_the_same_location():
    """Ни одной новой ячейки: обе формы обязаны находить ту же строку."""
    from apps.warehouse.services import resolve_storage_location

    location = StorageLocation.objects.create(
        name="Ячейка", code="S02-D03-C01", storage_allowed=True, is_active=True
    )
    by_legacy, _ = resolve_storage_location("S02-D03-C01")
    by_short, _ = resolve_storage_location("2-3-1")
    assert by_legacy is not None and by_short is not None
    assert by_legacy.pk == by_short.pk == location.pk
    assert StorageLocation.objects.count() == 1, "разрешение адреса создало дубликат"


def test_short_address_matches_the_product_examples():
    assert short_address("S01-D01-C01") == "1-1-1"
    assert short_address("S02-D03-C01") == "2-3-1"
    # Не-canonical адрес короткой формы не имеет и переписываться не должен.
    assert short_address("S03-L03-D02") == "S03-L03-D02"


def test_short_form_does_not_zero_pad_or_reorder():
    assert normalize_address_input("10-2-3") == "S10-D02-C03"
    assert normalize_address_input("1-2-5") == "S01-D02-C05"


@pytest.mark.django_db
def test_stored_ordering_stays_numeric_not_lexical():
    """Сортировка обязана идти по хранимому code с ведущими нулями.

    По короткой форме «1-10-1» встало бы раньше «1-2-1»."""
    for code in ("S01-D02-C01", "S01-D10-C01", "S01-D01-C01"):
        StorageLocation.objects.create(
            name=code, code=code, storage_allowed=True, is_active=True
        )
    ordered = [loc.short_code for loc in StorageLocation.objects.all()]
    assert ordered == ["1-1-1", "1-2-1", "1-10-1"], f"порядок ячеек нефизический: {ordered}"


# --- Stage 8: смешанных видимых форматов быть не должно --------------------------------


@pytest.mark.django_db
def test_cell_recount_screens_show_the_operator_address(client, boss):
    """§«Do not keep mixed visible formats».

    create_cell_recount сохраняет section_code=location.code (длинная форма), а
    экраны печатают его как есть. Везде 1-3-8, а здесь S01-D03-C08."""
    from apps.stocktaking.section_recount import create_cell_recount

    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-D03-C08", storage_allowed=True,
        is_active=True, level=StorageLocation.Level.CELL,
    )
    doc = create_cell_recount(location=location, by=boss)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("section_recount_detail", args=[doc.pk])).content.decode()
    assert "S01-D03-C08" not in html, "экран пересчёта показывает длинный адрес"
    assert "1-3-8" in html


# --- Stage 9: цена и её источник обязаны меняться вместе -------------------------------


@pytest.mark.django_db
def test_price_and_source_never_desync_on_a_failed_refresh(monkeypatch):
    """refresh_linked_part_prices пишет цену и price_source двумя отдельными
    bulk_update и сам не транзакционен. Управляющая команда и CLI-импорт
    внешней транзакции не открывают, поэтому сбой между записями оставляет
    новую цену с источником MANUAL - ровно то состояние, по которому отчёты
    принимают решение."""
    from apps.brp.models import BrpCatalogPart, BrpPartLink
    from apps.catalog import services as catalog_services
    from apps.catalog.models import Category, PartType, Unit

    unit = Unit.objects.get(name="Штука")
    cat = Category.objects.create(name="Вариатор")
    part = PartType.objects.create(
        name="Болт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("138496"),
    )
    brp_part = BrpCatalogPart.objects.create(
        material_no="700100", wholesale_price_usd=Decimal("1000"), is_current=True
    )
    BrpPartLink.objects.create(
        part=part, brp_part=brp_part, usd_rate_used=Decimal("100"),
        markup_percent_used=Decimal("0"),
        price_source=BrpPartLink.PriceSource.MANUAL,
        manual_customer_price_rub=Decimal("138496"),
    )

    def boom(links):
        raise RuntimeError("сбой между двумя записями")

    monkeypatch.setattr(catalog_services, "_relabel_overridden_links", boom)
    with pytest.raises(RuntimeError):
        catalog_services.refresh_linked_part_prices(
            usd_rate=Decimal("100"), brp_markup=Decimal("0"),
            polaris_markup=Decimal("0"), catalogs=frozenset({"brp"}),
        )

    part.refresh_from_db()
    link = BrpPartLink.objects.get(part=part)
    desynced = (
        part.recommended_price != Decimal("138496")
        and link.price_source == BrpPartLink.PriceSource.MANUAL
    )
    assert not desynced, (
        f"цена перезаписана ({part.recommended_price}), "
        f"а источник остался {link.price_source}"
    )
