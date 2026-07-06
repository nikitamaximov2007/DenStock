"""Layer 31 — продвижение позиции BRP-каталога в карточку склада и учёт наличия.

«Добавить в склад» создаёт ТОЛЬКО карточку (PartType + номера + связь со
снимком цены): остатков не появляется. «Учесть наличие» дополнительно открывает
черновик документа «Инвентаризация начальных остатков» (существующий workflow
поступления, Layer 28): количество, ячейка и себестоимость вводятся там, остаток
появляется только после «Провести поступление».
"""
from django.db import transaction

from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.procurement.models import money
from apps.receipts.models import Receipt
from apps.receipts.services import create_receipt
from apps.suppliers.models import Supplier

from .models import BrpCatalogPart, BrpPartLink, BrpPricingSettings
from .pricing import customer_price_rub

BRP_CATEGORY_NAME = "BRP"
DEFAULT_UNIT_NAME = "Штука"
INTAKE_SUPPLIER_NAME = "Стартовый ввод"
INTAKE_COMMENT = "Инвентаризация начальных остатков (BRP)"


class BrpPromotionError(Exception):
    """Позицию нельзя продвинуть в склад (нет справочников и т.п.)."""


def _default_unit() -> Unit:
    unit = Unit.objects.filter(name__iexact=DEFAULT_UNIT_NAME, is_active=True).first()
    if unit is None:
        unit = Unit.objects.filter(is_active=True).first()
    if unit is None:
        raise BrpPromotionError("В справочниках нет единиц измерения: добавьте хотя бы одну.")
    return unit


@transaction.atomic
def promote_to_warehouse(
    brp_part: BrpCatalogPart, *, by=None, manual_price=None
) -> PartType:
    """Создать карточку склада из позиции BRP. Идемпотентно, остатков НЕ создаёт.

    Цена: рассчитанная по текущим настройкам (курс/наценка фиксируются в
    связи навсегда); manual_price переопределяет итог, не затирая ни исходную
    цену BRP в долларах, ни рассчитанное значение.
    """
    existing = BrpPartLink.objects.filter(brp_part=brp_part).select_related("part").first()
    if existing is not None:
        return existing.part

    settings = BrpPricingSettings.get()
    calculated = customer_price_rub(
        brp_part.retail_price_usd, settings.brp_usd_rate, settings.brp_markup_percent
    )
    final = manual_price if manual_price is not None else calculated
    source = (
        BrpPartLink.PriceSource.MANUAL
        if manual_price is not None
        else BrpPartLink.PriceSource.CALCULATED
    )

    category, _ = Category.objects.get_or_create(
        name=BRP_CATEGORY_NAME, parent=None, defaults={"sort_order": 0}
    )
    manufacturer, _ = Manufacturer.objects.get_or_create(name=BRP_CATEGORY_NAME)
    name = brp_part.part_desc or f"BRP {brp_part.material_no}"
    part = PartType.objects.create(
        name=name[:200],
        category=category,
        manufacturer=manufacturer,
        unit=_default_unit(),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=money(final) if final is not None else None,
        description=f"Из BRP-каталога, номер {brp_part.material_no}."
                    f" Статус BRP: {brp_part.brp_status or 'нет'}.",
    )
    PartNumber.objects.create(
        part=part, value=brp_part.material_no, kind=PartNumber.Kind.OEM, is_primary=True
    )
    for replacement in (brp_part.replacement_no_1, brp_part.replacement_no_2):
        if replacement:
            PartNumber.objects.create(
                part=part, value=replacement, kind=PartNumber.Kind.ANALOG
            )

    BrpPartLink.objects.create(
        part=part,
        brp_part=brp_part,
        brp_retail_price_usd=brp_part.retail_price_usd,
        brp_wholesale_price_usd=brp_part.wholesale_price_usd,
        usd_rate_used=settings.brp_usd_rate,
        markup_percent_used=settings.brp_markup_percent,
        calculated_customer_price_rub=calculated,
        manual_customer_price_rub=manual_price,
        final_customer_price_rub=final,
        price_source=source,
        created_by=by,
    )
    return part


def find_promoted_part(brp_part: BrpCatalogPart):
    """Карточка склада для позиции BRP, если уже продвинута (иначе None)."""
    link = BrpPartLink.objects.filter(brp_part=brp_part).select_related("part").first()
    return link.part if link else None


def get_or_create_intake_draft(*, by) -> Receipt:
    """Черновик «Инвентаризации начальных остатков» пользователя (один активный).

    Это обычный документ поступления (Layer 28): черновик не меняет склад,
    остаток появится в выбранной ячейке после «Провести поступление».
    """
    draft = (
        Receipt.objects.filter(
            status=Receipt.Status.DRAFT,
            created_by=by,
            comment__startswith=INTAKE_COMMENT,
        )
        .order_by("-created_at")
        .first()
    )
    if draft is not None:
        return draft
    supplier, _ = Supplier.objects.get_or_create(
        name=INTAKE_SUPPLIER_NAME, defaults={"is_active": True}
    )
    return create_receipt(supplier=supplier, comment=INTAKE_COMMENT, by=by)
