from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.brp.models import BrpPartLink, BrpPricingSettings
from apps.brp.pricing import catalog_part_price_rub as brp_catalog_part_price_rub
from apps.counting.services import find_brp_price_source
from apps.inventory.presentation import EXACT_NUMBER_KINDS
from apps.polaris.models import PolarisPartLink, PolarisPricingSettings
from apps.polaris.pricing import customer_price_rub as polaris_customer_price_rub
from apps.polaris.services import find_polaris_price_source
from apps.procurement.models import money
from apps.warehouse.models import ValuationSettings

from .models import Category, PartNumber, PartType, Unit, normalize_number


@dataclass(frozen=True)
class CurrentPriceSettings:
    current_usd_rate: Decimal
    brp_markup_percent: Decimal
    polaris_markup_percent: Decimal
    updated_at: object
    updated_by: object | None


def get_current_price_settings(*, create: bool = True) -> CurrentPriceSettings:
    """Read shared pricing settings, optionally without creating singletons.

    Dry-run management commands must not turn a missing configuration row into
    a database write merely by reading the defaults.
    """
    valuation = (
        ValuationSettings.get()
        if create
        else ValuationSettings.objects.filter(pk=1).first() or ValuationSettings()
    )
    brp = (
        BrpPricingSettings.get()
        if create
        else BrpPricingSettings.objects.filter(pk=1).first() or BrpPricingSettings()
    )
    polaris = (
        PolarisPricingSettings.get()
        if create
        else PolarisPricingSettings.objects.filter(pk=1).first() or PolarisPricingSettings()
    )
    return CurrentPriceSettings(
        current_usd_rate=valuation.current_usd_rate,
        brp_markup_percent=brp.brp_markup_percent,
        polaris_markup_percent=polaris.polaris_markup_percent,
        updated_at=valuation.updated_at,
        updated_by=valuation.updated_by,
    )


def _locked_singleton(model):
    obj = model.objects.select_for_update().filter(pk=1).first()
    if obj is None:
        obj = model.objects.create(pk=1)
    return obj


def _brp_link_price(link: BrpPartLink, usd_rate: Decimal, markup: Decimal):
    if not link.brp_part.is_current:
        return None
    source = find_brp_price_source(link.brp_part.material_no_norm, link.brp_part)
    if source is None:
        return None
    # Источник цены это позиция каталога: надбавку VIN применяет слой цен.
    return brp_catalog_part_price_rub(source, usd_rate, markup)


def _polaris_link_price(link: PolarisPartLink, usd_rate: Decimal, markup: Decimal):
    source = find_polaris_price_source(link.polaris_part.part_number_norm, link.polaris_part)
    if source is None:
        return None
    return polaris_customer_price_rub(source.wholesale_price_usd, usd_rate, markup)


@dataclass
class LinkedPriceRefreshPlan:
    """A non-mutating plan for current linked catalog prices.

    Only ``PartType.recommended_price`` is deliberately eligible for updates.
    Link records preserve their promotion-time snapshots and sale documents are
    outside this service entirely.
    """

    parts_to_update: dict[int, object] = field(default_factory=dict)
    calculated_links: int = 0
    unchanged: int = 0
    skipped_without_wholesale: int = 0
    skipped_manual: int = 0
    brp_links: int = 0
    polaris_links: int = 0

    @property
    def updated(self) -> int:
        return len(self.parts_to_update)


def plan_linked_part_price_refresh(
    *,
    usd_rate: Decimal,
    brp_markup: Decimal,
    polaris_markup: Decimal,
    catalogs: frozenset[str] | None = None,
) -> LinkedPriceRefreshPlan:
    """Build a dry-run-safe plan using current wholesale catalog prices."""
    selected_catalogs = catalogs or frozenset({"brp", "polaris"})
    unknown = selected_catalogs - {"brp", "polaris"}
    if unknown:
        raise ValueError(f"Неизвестный каталог для пересчёта: {', '.join(sorted(unknown))}")

    plan = LinkedPriceRefreshPlan()
    if "brp" in selected_catalogs:
        brp_links = BrpPartLink.objects.select_related("brp_part", "part")
        plan.skipped_manual += brp_links.filter(
            price_source=BrpPartLink.PriceSource.MANUAL
        ).count()
        for link in brp_links.filter(price_source=BrpPartLink.PriceSource.CALCULATED):
            plan.brp_links += 1
            if not link.brp_part.is_current:
                if link.part.recommended_price is not None:
                    link.part.recommended_price = None
                    plan.parts_to_update[link.part_id] = link.part
                plan.skipped_without_wholesale += 1
                continue
            price = _brp_link_price(link, usd_rate, brp_markup)
            if price is None or price <= 0:
                plan.skipped_without_wholesale += 1
                continue
            plan.calculated_links += 1
            recommended = money(price)
            if link.part.recommended_price == recommended:
                plan.unchanged += 1
                continue
            link.part.recommended_price = recommended
            plan.parts_to_update[link.part_id] = link.part

    if "polaris" in selected_catalogs:
        polaris_links = PolarisPartLink.objects.select_related("polaris_part", "part")
        plan.skipped_manual += polaris_links.filter(
            price_source=PolarisPartLink.PriceSource.MANUAL
        ).count()
        for link in polaris_links.filter(price_source=PolarisPartLink.PriceSource.CALCULATED):
            plan.polaris_links += 1
            price = _polaris_link_price(link, usd_rate, polaris_markup)
            if price is None or price <= 0:
                plan.skipped_without_wholesale += 1
                continue
            plan.calculated_links += 1
            recommended = money(price)
            if link.part.recommended_price == recommended:
                plan.unchanged += 1
                continue
            link.part.recommended_price = recommended
            plan.parts_to_update[link.part_id] = link.part
    return plan


def refresh_linked_part_prices(
    *,
    usd_rate: Decimal,
    brp_markup: Decimal,
    polaris_markup: Decimal,
    catalogs: frozenset[str] | None = None,
) -> int:
    """Apply current-price updates without changing historical snapshots.

    Missing or non-positive wholesale prices leave the existing recommended
    price intact. This prevents a partial supplier price file from zeroing a
    sale suggestion on an already-linked warehouse card.
    """
    plan = plan_linked_part_price_refresh(
        usd_rate=usd_rate,
        brp_markup=brp_markup,
        polaris_markup=polaris_markup,
        catalogs=catalogs,
    )
    if plan.parts_to_update:
        from apps.catalog.models import PartType

        PartType.objects.bulk_update(plan.parts_to_update.values(), ["recommended_price"])
    return plan.updated


@transaction.atomic
def update_current_price_settings(
    *,
    current_usd_rate: Decimal,
    brp_markup_percent: Decimal,
    polaris_markup_percent: Decimal,
    by=None,
) -> tuple[CurrentPriceSettings, int]:
    valuation = _locked_singleton(ValuationSettings)
    brp = _locked_singleton(BrpPricingSettings)
    polaris = _locked_singleton(PolarisPricingSettings)

    valuation.current_usd_rate = current_usd_rate
    valuation.updated_by = by
    valuation.save(update_fields=["current_usd_rate", "updated_by", "updated_at"])

    brp.brp_markup_percent = brp_markup_percent
    brp.updated_by = by
    brp.save(update_fields=["brp_markup_percent", "updated_by", "updated_at"])

    polaris.polaris_markup_percent = polaris_markup_percent
    polaris.updated_by = by
    polaris.save(update_fields=["polaris_markup_percent", "updated_by", "updated_at"])

    refreshed = refresh_linked_part_prices(
        usd_rate=current_usd_rate,
        brp_markup=brp_markup_percent,
        polaris_markup=polaris_markup_percent,
    )
    return get_current_price_settings(), refreshed
# --- Ручное добавление детали -----------------------------------------------

# Категория у карточки обязательна, а на новой системе категорий может не быть
# ни одной. Оператору неоткуда её взять, поэтому она заводится сама - так же,
# как это делает продвижение позиции из каталога поставщика.
MANUAL_CATEGORY_NAME = "Добавлено вручную"
DEFAULT_UNIT_NAME = "Штука"


class ManualPartError(ValueError):
    """Понятная человеку причина, по которой деталь не создана."""


def _manual_unit() -> Unit:
    unit = Unit.objects.filter(name__iexact=DEFAULT_UNIT_NAME, is_active=True).first()
    if unit is None:
        unit = Unit.objects.filter(is_active=True).first()
    if unit is None:
        raise ManualPartError(
            "В справочниках нет ни одной единицы измерения. Добавьте хотя бы одну."
        )
    return unit


def find_parts_by_article(article: str):
    """Детали с таким же артикулом. Пусто, если артикул не задан.

    Уникальности у номера в модели нет, и вводить её здесь нельзя: у разных
    производителей номера совпадают, а на существующих данных такое правило
    просто не применилось бы. Но оператору стоит показать, что деталь с этим
    артикулом уже есть: почти всегда ему нужна именно она.
    """
    normalized = normalize_number(article or "")
    if not normalized:
        return PartType.objects.none()
    return PartType.objects.select_related("category").filter(
        numbers__normalized_value=normalized,
        numbers__kind__in=EXACT_NUMBER_KINDS,
    ).distinct()


def clean_manual_price(value) -> Decimal | None:
    """Разобрать введённую цену. Пустое поле - это отсутствие цены, а не ноль."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        price = Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ManualPartError("Цена должна быть числом.") from exc
    if not price.is_finite():
        raise ManualPartError("Цена должна быть конечным числом.")
    if price < 0:
        raise ManualPartError("Цена не может быть отрицательной.")
    try:
        return price.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        # Округление отказывает на слишком длинном числе. Через форму такое не
        # пройдёт, но служба вызывается и из кода, и падать здесь ей нечем.
        raise ManualPartError("Цена слишком велика.") from exc


@transaction.atomic
def create_manual_part(*, name: str, article: str = "", price=None) -> PartType:
    """Завести карточку детали вручную. Остатков НЕ создаёт.

    Повторяет то, что делает продвижение позиции из каталога поставщика:
    карточка, единица «Штука», учёт количеством и, если цена указана, она же
    рекомендуемая цена продажи. Появление карточки не означает, что деталь
    физически есть на складе: остаток создаёт только приёмка.

    Артикул необязателен, потому что необязателен и в модели. Без него деталь
    будет находиться только по названию.
    """
    clean_name = " ".join((name or "").split())
    if not clean_name:
        raise ManualPartError("Укажите название детали.")

    clean_article = (article or "").strip()
    recommended = clean_manual_price(price)

    category, _ = Category.objects.get_or_create(
        name=MANUAL_CATEGORY_NAME, parent=None, defaults={"sort_order": 100}
    )
    part = PartType.objects.create(
        name=clean_name[:200],
        category=category,
        unit=_manual_unit(),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=recommended,
    )
    if clean_article:
        # Вид «артикул» наравне с OEM считается точным номером, поэтому деталь
        # находится поиском и показывается с артикулом в списках и отчётах.
        PartNumber.objects.create(
            part=part,
            value=clean_article[:100],
            kind=PartNumber.Kind.ARTICLE,
            is_primary=True,
        )
    return part
