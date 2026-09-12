"""P0 перед запуском PRO-STOR: клиент видит цену, посчитанную из оптовой.

Бизнес-правило одно: цена клиента = оптовая цена каталога × курс × (1 + наценка),
до целого рубля ROUND_HALF_UP. При курсе 105 и наценке 40% это 100 $ -> 14 700 ₽.
Сама формула закреплена в tests/test_brp.py, здесь проверяется то, что нельзя
проверить в юните: путь от оптовой цены прайса до цифры на публичной странице,
в корзине, в форме заявки и в сохранённом снимке заявки.

Что гарантируется:

* у PRO-STOR нет своей и нет устаревшей формулы: все публичные поверхности
  показывают одну и ту же каноническую цену карточки;
* цена, присланная браузером, не авторитетна нигде;
* при продвижении позиции каталога цена берётся из того же источника, что и при
  пересчёте: у позиции с нулевой оптовой ценой - из цепочки замен;
* аудит цен (`manage.py audit_customer_prices`) отличает совпадение от
  расхождения, ручной цены и «сверить нечем», и ничего не пишет.
"""

from decimal import Decimal

import pytest
from django.core.cache import cache
from django.core.management import call_command

from apps.brp.models import BrpCatalogPart, BrpPartLink, BrpPricingSettings
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import PartType
from apps.catalog.price_audit import (
    CUSTOMER_PRICE_MISSING,
    EXACT_MATCH,
    FORMULA_NOT_APPLICABLE,
    MANUAL_OVERRIDE,
    PRICE_MISMATCH,
    SOURCE_INVALID,
    WHOLESALE_SOURCE_MISSING,
    audit_prices,
)
from apps.catalog.public_contracts import resolve_current_customer_price
from apps.catalog.services import (
    certify_valid_manual_price_exception,
    get_current_price_settings,
    refresh_linked_part_prices,
)
from apps.customer_requests.models import CustomerRequest
from apps.warehouse.models import ValuationSettings

RATE = Decimal("105")
MARKUP = Decimal("40")
NBSP = " "


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def pricing(db):
    """Курс и наценка бизнес-правила: 105 ₽/$ и 40%."""
    valuation = ValuationSettings.get()
    valuation.current_usd_rate = RATE
    valuation.save(update_fields=["current_usd_rate", "updated_at"])
    settings = BrpPricingSettings.get()
    settings.brp_markup_percent = MARKUP
    settings.save(update_fields=["brp_markup_percent", "updated_at"])
    return get_current_price_settings()


def _catalog_part(material_no, wholesale, *, retail="0", replacement="", status=""):
    return BrpCatalogPart.objects.create(
        material_no=material_no,
        part_desc=f"PART {material_no}",
        retail_price_usd=Decimal(retail),
        wholesale_price_usd=Decimal(wholesale) if wholesale is not None else None,
        replacement_no_1=replacement,
        brp_status=status,
        is_current=True,
    )


def _publish(part, public_catalog, quantity="3"):
    part.is_public = True
    part.is_active = True
    part.save(update_fields=["is_public", "is_active"])
    public_catalog.stock(part, quantity)
    return part


# --- Оптовая цена -> цена карточки ---------------------------------------------------------


def test_promotion_prices_the_card_from_the_wholesale_price(pricing, admin_user):
    part = promote_to_warehouse(_catalog_part("PRICE-100", "100"), by=admin_user)

    assert part.recommended_price == Decimal("14700.00")
    assert part.brp_link.price_source == BrpPartLink.PriceSource.CALCULATED


def test_promotion_uses_the_same_price_source_as_the_recalculation(pricing, admin_user):
    """Позиция с нулевой оптовой: цена берётся из замены, как и при пересчёте.

    Раньше карточка продвигалась совсем без цены, хотя цена в каталоге есть, и
    оператору приходилось вписывать её руками - а следующий пересчёт эту ручную
    цену молча заменял расчётной.
    """
    zero = _catalog_part("250000059", "0")
    _catalog_part("250000418", "3.29", retail="4.19", replacement="250000059")

    part = promote_to_warehouse(zero, by=admin_user)

    assert part.recommended_price == Decimal("484.00")  # 3.29 × 105 × 1.4 = 483.63
    assert part.brp_link.brp_wholesale_price_usd == Decimal("0")  # цены самой позиции
    assert refresh_linked_part_prices(
        usd_rate=RATE, brp_markup=MARKUP, polaris_markup=MARKUP
    ) == 0, "пересчёт сразу после продвижения ничего не меняет"


def test_a_withdrawn_catalog_row_is_never_a_price_source(pricing, admin_user):
    """Цена не берётся из строки, которую поставщик больше не публикует.

    Тогда цены у карточки нет, и публичный каталог говорит «уточняется», а не
    подставляет цену позиции, которой в прайсе больше нет.
    """
    zero = _catalog_part("250000059", "0")
    stale = _catalog_part("250000418", "3.29", replacement="250000059")
    stale.is_current = False
    stale.save(update_fields=["is_current"])

    part = promote_to_warehouse(zero, by=admin_user)

    assert resolve_current_customer_price(part).status == "clarify"
    assert not part.recommended_price


# --- Цена карточки -> публичная цена -------------------------------------------------------


def test_every_public_surface_shows_the_canonical_price(
    pricing, admin_user, public_client, public_catalog
):
    catalog_part = _catalog_part("420931785", "100")
    part = _publish(promote_to_warehouse(catalog_part, by=admin_user), public_catalog)
    expected = Decimal("14700.00")
    shown = f"14{NBSP}700{NBSP}₽"

    assert resolve_current_customer_price(part).price_rub == expected

    search = public_client.get("/search/?q=420931785").content.decode()
    assert shown in search

    detail = public_client.get(f"/parts/{part.public_id}/").content.decode()
    assert shown in detail

    public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
    cart = public_client.get("/cart/").content.decode()
    assert shown in cart

    form = public_client.get("/request/").content.decode()
    assert shown in form

    token = form.split('name="submission_key" value="', 1)[1].split('"', 1)[0]
    response = public_client.post(
        "/request/submit/",
        {
            "submission_key": token,
            "customer_name": "Иван Петров",
            "customer_phone": "9001234567",
            "preferred_messenger": "telegram",
            "comment": "",
            "consent": "1",
            # Цена из браузера: не авторитетна нигде.
            "price": "1",
            "price_seen": "1",
            "total": "1",
        },
    )

    assert response.status_code == 302, response.status_code
    line = CustomerRequest.objects.get().lines.get()
    assert line.price_seen == expected


def test_the_public_price_follows_a_new_wholesale_price_without_a_second_formula(
    pricing, admin_user, public_client, public_catalog
):
    """Одна цена на весь проект: меняется опт - меняется публичная страница."""
    catalog_part = _catalog_part("420931786", "100")
    part = _publish(promote_to_warehouse(catalog_part, by=admin_user), public_catalog)
    assert f"14{NBSP}700{NBSP}₽" in public_client.get(f"/parts/{part.public_id}/").content.decode()

    catalog_part.wholesale_price_usd = Decimal("120")
    catalog_part.save(update_fields=["wholesale_price_usd"])
    refresh_linked_part_prices(usd_rate=RATE, brp_markup=MARKUP, polaris_markup=MARKUP)

    part.refresh_from_db()
    assert part.recommended_price == Decimal("17640.00")  # 120 × 105 × 1.4
    detail = public_client.get(f"/parts/{part.public_id}/").content.decode()
    assert f"17{NBSP}640{NBSP}₽" in detail
    assert f"14{NBSP}700{NBSP}₽" not in detail, "устаревшей копии цены нигде нет"


def test_rebuild_manual_exception_never_inherits_replacement_wholesale_price(
    pricing, admin_user, public_client, public_catalog
):
    """421000667 is a separate rebuild item, not the new replacement part."""
    rebuild = _catalog_part("421000667", "0")
    _catalog_part("421000668", "410.78", replacement="421000667")
    part = promote_to_warehouse(rebuild, by=admin_user, manual_price=Decimal("45000"))
    certify_valid_manual_price_exception(part)
    _publish(part, public_catalog)

    refresh_linked_part_prices(usd_rate=RATE, brp_markup=MARKUP, polaris_markup=MARKUP)
    part.refresh_from_db()
    assert part.recommended_price == Decimal("45000")
    assert part.certified_price_rub is None
    assert part.price_provenance == PartType.PriceProvenance.VALID_MANUAL_EXCEPTION
    assert resolve_current_customer_price(part).price_rub == Decimal("45000")

    shown = f"45{NBSP}000{NBSP}₽"
    assert shown in public_client.get(f"/parts/{part.public_id}/").content.decode()
    public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
    assert shown in public_client.get("/cart/").content.decode()
    form = public_client.get("/request/").content.decode()
    assert shown in form
    token = form.split('name="submission_key" value="', 1)[1].split('"', 1)[0]
    response = public_client.post(
        "/request/submit/",
        {
            "submission_key": token,
            "customer_name": "Проверка rebuild",
            "customer_phone": "9001234567",
            "preferred_messenger": "telegram",
            "comment": "",
            "consent": "1",
        },
    )
    assert response.status_code == 302
    assert CustomerRequest.objects.get().lines.get().price_seen == Decimal("45000")


def test_public_catalog_clarifies_an_unverified_numeric_price(pricing, admin_user):
    part = promote_to_warehouse(_catalog_part("UNVERIFIED-PRICE", "100"), by=admin_user)
    PartType.objects.filter(pk=part.pk).update(
        recommended_price=Decimal("16000"),
        certified_price_rub=None,
        price_provenance=PartType.PriceProvenance.UNVERIFIED,
    )
    part.refresh_from_db()

    assert resolve_current_customer_price(part).status == "clarify"


# --- Аудит --------------------------------------------------------------------------------


def _audit():
    return audit_prices(usd_rate=RATE, brp_markup=MARKUP, polaris_markup=MARKUP)


def test_the_audit_recognises_an_exact_match(pricing, admin_user):
    promote_to_warehouse(_catalog_part("AUDIT-OK", "100"), by=admin_user)

    report = _audit()

    assert report.by_category[EXACT_MATCH] == 1
    assert report.mismatches == []


def test_the_audit_names_a_wrong_price_with_its_source_and_delta(pricing, admin_user):
    part = promote_to_warehouse(_catalog_part("AUDIT-BAD", "100"), by=admin_user)
    PartType.objects.filter(pk=part.pk).update(recommended_price=Decimal("9000.00"))

    row = _audit().mismatches[0]

    assert row.part_id == part.pk
    assert row.source == "brp" and row.source_reference == "AUDIT-BAD"
    assert row.wholesale_usd == Decimal("100")
    assert (row.actual_price, row.expected_price) == (Decimal("9000.00"), Decimal("14700.00"))
    assert row.delta == Decimal("-5700.00")
    assert row.delta_percent == Decimal("-38.78")


def test_the_audit_does_not_call_a_rounding_difference_a_wrong_price(pricing, admin_user):
    part = promote_to_warehouse(_catalog_part("AUDIT-ROUND", "99.99"), by=admin_user)
    # 99.99 × 105 × 1.4 = 14698.53 -> 14699 ₽; усечение дало бы 14698.
    PartType.objects.filter(pk=part.pk).update(recommended_price=Decimal("14698.00"))

    report = _audit()

    assert report.by_category[PRICE_MISMATCH] == 0
    assert report.by_category["ROUNDING_ONLY_MATCH"] == 1


def test_the_audit_separates_a_manual_price_from_a_wrong_one(pricing, admin_user):
    promote_to_warehouse(
        _catalog_part("AUDIT-MANUAL", "100"), by=admin_user, manual_price=Decimal("20000")
    )

    report = _audit()

    assert report.by_category[MANUAL_OVERRIDE] == 1
    assert report.by_category[PRICE_MISMATCH] == 0
    assert report.rows[0].manual is True


def test_the_audit_says_when_there_is_nothing_to_check_against(pricing, admin_user):
    promote_to_warehouse(_catalog_part("AUDIT-ZERO", "0"), by=admin_user)
    withdrawn = promote_to_warehouse(_catalog_part("AUDIT-GONE", "100"), by=admin_user)
    BrpCatalogPart.objects.filter(material_no="AUDIT-GONE").update(is_current=False)
    PartType.objects.create(
        name="Деталь без каталога",
        category=withdrawn.category,
        unit=withdrawn.unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("1500.00"),
    )

    report = _audit()

    assert report.by_category[SOURCE_INVALID] == 1
    assert report.by_category[WHOLESALE_SOURCE_MISSING] == 1
    assert report.by_category[FORMULA_NOT_APPLICABLE] == 1
    assert report.by_category[PRICE_MISMATCH] == 0


def test_the_audit_reports_a_missing_customer_price(pricing, admin_user):
    part = promote_to_warehouse(_catalog_part("AUDIT-NOPRICE", "100"), by=admin_user)
    PartType.objects.filter(pk=part.pk).update(recommended_price=None)

    assert _audit().by_category[CUSTOMER_PRICE_MISSING] == 1


def test_the_audit_writes_nothing(pricing, admin_user, django_assert_num_queries):
    part = promote_to_warehouse(_catalog_part("AUDIT-READONLY", "100"), by=admin_user)
    PartType.objects.filter(pk=part.pk).update(recommended_price=Decimal("1.00"))
    before = PartType.objects.values_list("id", "recommended_price").order_by("id")
    snapshot = list(before)

    call_command("audit_customer_prices", "--show", "0")

    assert list(PartType.objects.values_list("id", "recommended_price").order_by("id")) == snapshot
    assert BrpPartLink.objects.get(part=part).price_source == BrpPartLink.PriceSource.CALCULATED


def test_the_audit_counts_public_and_in_stock_separately(
    pricing, admin_user, public_catalog
):
    priced_in_stock = _publish(
        promote_to_warehouse(_catalog_part("AUDIT-STOCK", "100"), by=admin_user), public_catalog
    )
    PartType.objects.filter(pk=priced_in_stock.pk).update(recommended_price=Decimal("100.00"))
    hidden = promote_to_warehouse(_catalog_part("AUDIT-HIDDEN", "100"), by=admin_user)
    PartType.objects.filter(pk=hidden.pk).update(
        recommended_price=Decimal("100.00"), is_public=False
    )

    report = _audit()

    assert report.public_with_price == 1
    assert report.in_stock_with_price == 1
    assert report.in_stock_by_category[PRICE_MISMATCH] == 1
    assert len(report.in_stock_mismatches) == 1
