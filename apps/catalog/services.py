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

from .models import (
    Category,
    Manufacturer,
    PartAnalog,
    PartBarcode,
    PartNumber,
    PartType,
    Unit,
    normalize_number,
)


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
def create_manual_part(
    *, name: str, article: str = "", price=None, barcode: str = "",
    manufacturer_name: str = "",
) -> PartType:
    """Завести карточку детали вручную. Остатков НЕ создаёт.

    Повторяет то, что делает продвижение позиции из каталога поставщика:
    карточка, единица «Штука», учёт количеством и, если цена указана, она же
    рекомендуемая цена продажи. Появление карточки не означает, что деталь
    физически есть на складе: остаток создаёт только приёмка.

    Артикул необязателен, потому что необязателен и в модели. Без него деталь
    будет находиться только по названию.

    Штрихкод спрашивается здесь же, потому что коробка у оператора в руках
    именно сейчас. Отдельным шагом он почти никогда не доходит до карточки, и
    деталь остаётся неотсканируемой.
    """
    clean_name = " ".join((name or "").split())
    if not clean_name:
        raise ManualPartError("Укажите название детали.")

    clean_article = (article or "").strip()
    clean_barcode = (barcode or "").strip()
    recommended = clean_manual_price(price)
    assert_barcode_is_free(clean_barcode)

    category, _ = Category.objects.get_or_create(
        name=MANUAL_CATEGORY_NAME, parent=None, defaults={"sort_order": 100}
    )
    manufacturer = None
    clean_manufacturer = " ".join((manufacturer_name or "").split())
    if clean_manufacturer:
        manufacturer, _ = Manufacturer.objects.get_or_create(name=clean_manufacturer[:150])
    part = PartType.objects.create(
        name=clean_name[:200],
        category=category,
        manufacturer=manufacturer,
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
    if clean_barcode:
        PartBarcode.objects.create(part=part, value=clean_barcode[:100])
    return part
def assert_barcode_is_free(barcode: str) -> None:
    """Штрихкод в модели уникален. Занятый - повод показать, кем именно.

    Оператору бесполезно сообщение «такое значение уже есть»: ему нужно знать,
    на какой детали оно висит, чтобы понять, ту ли коробку он держит.
    """
    value = (barcode or "").strip()
    if not value:
        return
    taken = PartBarcode.objects.select_related("part").filter(value=value).first()
    if taken is not None:
        raise ManualPartError(
            f"Штрихкод {value} уже стоит на детали «{taken.part.name}». "
            "Проверьте, не эта ли деталь у вас в руках."
        )


# --- Аналоги ------------------------------------------------------------------


class AnalogLinkError(ValueError):
    """Понятная человеку причина, по которой связь не создана."""


@transaction.atomic
def link_analog(*, original: PartType, analog: PartType, note: str = "", by=None):
    """Отметить одну деталь аналогом другой. Возвращает пару (связь, создана).

    Повторный вызов ничего не удваивает: это нужно и оператору, который нажал
    дважды, и импорту каталога, который могут запустить тем же файлом.
    """
    if original.pk == analog.pk:
        raise AnalogLinkError("Деталь не может быть аналогом самой себя.")

    reverse = PartAnalog.objects.filter(original=analog, analog=original).first()
    if reverse is not None:
        # Тот же факт с другой стороны. Вторая запись показала бы одну и ту же
        # пару и в «Аналогах», и в «Аналог для», и человек решил бы, что это
        # разные связи.
        raise AnalogLinkError(
            f"«{original.name}» уже отмечена как аналог детали «{analog.name}». "
            "Обратная связь заводится отдельно только вместе со снятием прежней."
        )

    link, created = PartAnalog.objects.get_or_create(
        original=original,
        analog=analog,
        defaults={"note": (note or "").strip()[:255], "created_by": by},
    )
    return link, created


def unlink_analog(link: PartAnalog) -> None:
    """Снять связь. Сами детали остаются: это отдельные складские карточки."""
    link.delete()


def analog_links_of(part: PartType):
    """Связи, где деталь выступает исходной: её аналоги."""
    return (
        PartAnalog.objects.filter(original=part)
        .select_related("analog", "analog__category", "analog__manufacturer")
    )


def original_links_of(part: PartType):
    """Связи, где деталь выступает аналогом: для чего она подходит."""
    return (
        PartAnalog.objects.filter(analog=part)
        .select_related("original", "original__category", "original__manufacturer")
    )


def analog_rows(part: PartType, *, direction: str = "analogs") -> list[dict]:
    """Строки для карточки: деталь, артикул, цена и сколько сейчас на складе.

    Наличие считается тем же способом, что и в поиске, и одним запросом на все
    строки сразу: карточка с двумя десятками аналогов не должна превращаться в
    два десятка обращений к базе.
    """
    from decimal import Decimal

    from apps.inventory.movement import live_stock_rows
    from apps.inventory.presentation import (
        manufacturer_display,
        part_exact_number,
        with_part_identity,
    )

    links = list(
        analog_links_of(part) if direction == "analogs" else original_links_of(part)
    )
    if not links:
        return []

    other_ids = [
        (link.analog_id if direction == "analogs" else link.original_id) for link in links
    ]
    # Номера подтягиваются одним prefetch на весь набор: точный артикул иначе
    # стоил бы запроса на каждую строку.
    by_id = {
        item.pk: item
        for item in with_part_identity(
            PartType.objects.filter(pk__in=other_ids).select_related(
                "category", "manufacturer"
            ),
            part_field="",
        )
    }

    stock: dict[int, list] = {}
    for row in live_stock_rows(part_ids=other_ids):
        stock.setdefault(row.part_type.pk, []).append(row)

    zero = Decimal("0")
    rows = []
    for link, other_id in zip(links, other_ids, strict=True):
        item = by_id.get(other_id)
        if item is None:
            continue
        locations = stock.get(other_id, [])
        rows.append({
            "link": link,
            "part": item,
            "exact_number": part_exact_number(item, default=""),
            "manufacturer": manufacturer_display(item),
            "price": item.recommended_price,
            "available": sum((row.available for row in locations), zero),
            "locations": [row.location.code for row in locations],
            "note": link.note,
        })
    return rows


@transaction.atomic
def create_analog_part(
    *, original: PartType, name: str, article: str = "", price=None,
    barcode: str = "", manufacturer_name: str = "", note: str = "", by=None,
) -> PartType:
    """Завести новую деталь и сразу отметить её аналогом исходной.

    Карточка создаётся тем же путём, что и любая ручная деталь: второй копии
    этой логики нет. Обе записи в одной транзакции - иначе появилась бы деталь
    без связи, а человек считал бы, что ничего не произошло.
    """
    part = create_manual_part(
        name=name, article=article, price=price,
        barcode=barcode, manufacturer_name=manufacturer_name,
    )
    link_analog(original=original, analog=part, note=note, by=by)
    return part
