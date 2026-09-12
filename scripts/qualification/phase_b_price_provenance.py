"""Phase B data readiness: classify production prices and Russian names.

Runs against an ISOLATED clone of the production snapshot. Never touches
production. The classification is produced by the canonical pricing planner
(`plan_linked_part_price_refresh`), not by a second formula: the script only
reads what the planner would decide and turns it into review tables.
"""
import argparse
import csv
import json
from collections import Counter
from decimal import Decimal

import django

django.setup()

from apps.actions.models import PartCustomsInfo  # noqa: E402
from apps.brp.models import BrpPartLink  # noqa: E402
from apps.brp.pricing import effective_wholesale_usd  # noqa: E402
from apps.catalog.models import PartType  # noqa: E402
from apps.catalog.public_catalog import public_parts  # noqa: E402
from apps.catalog.services import (  # noqa: E402
    certify_valid_manual_price_exception,
    get_current_price_settings,
    plan_linked_part_price_refresh,
)
from apps.catalog_import.models import AftermarketCatalogPart  # noqa: E402
from apps.counting.services import find_brp_price_source  # noqa: E402
from apps.inventory.movement import live_stock_rows  # noqa: E402
from apps.inventory.presentation import (  # noqa: E402
    manufacturer_display,
    part_exact_number,
    with_part_identity,
)
from apps.polaris.models import PolarisPartLink  # noqa: E402

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def guard(args, *, writes: bool):
    """Тот же замок, что у остальных qualification-скриптов проекта.

    Скрипт рассчитан только на одноразовую копию снимка на рабочей машине.
    Продакшен недостижим по построению: чужой хост и чужое имя базы
    отвергаются до первого запроса.
    """
    from django.conf import settings
    from django.db import connection

    database = settings.DATABASES["default"]
    if not args.confirm_isolated:
        raise SystemExit("Refusing to run without --confirm-isolated.")
    if connection.vendor != "postgresql":
        raise SystemExit("This harness needs PostgreSQL 16.")
    if (database.get("HOST") or "") not in LOOPBACK:
        raise SystemExit(f"Refusing a non-loopback database host: {database.get('HOST')!r}")
    if database.get("NAME") != args.expect_database:
        raise SystemExit(
            f"Connected to {database.get('NAME')!r}, expected {args.expect_database!r}."
        )
    if writes:
        print(f"writing owner-confirmed exceptions into the isolated copy {database['NAME']!r}")

ZERO = Decimal("0")
OWNER_CONFIRMED_EXCEPTIONS = {"421000667"}  # rebuild cylinder, owner decision

FORMULA_CERTIFIED = "FORMULA_CERTIFIED"
VALID_MANUAL_EXCEPTION = "VALID_MANUAL_EXCEPTION"
UNVERIFIED = "UNVERIFIED"
SOURCE_MISSING = "SOURCE_MISSING"
NOT_APPLICABLE = "NOT_APPLICABLE"
TRUE_MISMATCH = "TRUE_MISMATCH"
ORDER = [
    FORMULA_CERTIFIED,
    VALID_MANUAL_EXCEPTION,
    TRUE_MISMATCH,
    UNVERIFIED,
    SOURCE_MISSING,
    NOT_APPLICABLE,
]
SHOWABLE = {FORMULA_CERTIFIED, VALID_MANUAL_EXCEPTION}

CONFIRM_MANUAL = "CONFIRM MANUAL PRICE"
USE_FORMULA = "USE FORMULA PRICE"
NEED_SOURCE = "NEED SOURCE"
NOT_APPLICABLE_ACTION = "NOT APPLICABLE"
HUMAN = "NEEDS HUMAN REVIEW"


def availability():
    totals: Counter = Counter()
    for row in live_stock_rows():
        totals[row.part_type.pk] += row.available
    return {pid: qty for pid, qty in totals.items() if qty > ZERO}


def own_wholesale_map():
    """Собственная оптовая цена позиции каталога и справочная цена замены.

    Собственная это то, что сертифицирует цену по канону 7d708e4. Замена -
    только справка для оператора: она не доказывает цену отдельной товарной
    сущности (например rebuild).
    """
    own, replacement, source_kind = {}, {}, {}
    for link in BrpPartLink.objects.select_related("brp_part").iterator(chunk_size=2000):
        catalog_part = link.brp_part
        source_kind[link.part_id] = ("brp", catalog_part.material_no, link.price_source)
        raw = catalog_part.wholesale_price_usd
        if catalog_part.is_current and raw and raw > ZERO:
            own[link.part_id] = effective_wholesale_usd(catalog_part)
        chain = find_brp_price_source(catalog_part.material_no_norm, catalog_part)
        if chain is not None and chain.pk != catalog_part.pk:
            wholesale = effective_wholesale_usd(chain)
            if wholesale and wholesale > ZERO:
                replacement[link.part_id] = (chain.material_no, wholesale)
    for link in PolarisPartLink.objects.select_related("polaris_part").iterator(chunk_size=2000):
        catalog_part = link.polaris_part
        source_kind[link.part_id] = ("polaris", catalog_part.part_number, link.price_source)
        raw = catalog_part.wholesale_price_usd
        if raw and raw > ZERO:
            own[link.part_id] = raw
    for row in AftermarketCatalogPart.objects.values_list(
        "part_id", "manufacturer_number", "dealer_cost_usd"
    ).iterator(chunk_size=5000):
        part_id, number, cost = row
        source_kind[part_id] = ("aftermarket", number or "", "")
        if cost and cost > ZERO:
            own[part_id] = cost
    return own, replacement, source_kind


def formula_price(wholesale, rate, markup):
    if wholesale in (None, ""):
        return None
    from apps.brp.pricing import customer_price_rub
    from apps.procurement.models import money

    price = customer_price_rub(wholesale, rate, markup)
    return money(price) if price is not None and price > ZERO else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="Куда положить артефакты.")
    parser.add_argument("--confirm-isolated", action="store_true")
    parser.add_argument("--expect-database", required=True)
    args = parser.parse_args()
    guard(args, writes=True)

    pricing = get_current_price_settings(create=False)
    rate, brp_markup = pricing.current_usd_rate, pricing.brp_markup_percent
    polaris_markup = pricing.polaris_markup_percent

    before = {
        pk: (price, provenance)
        for pk, price, provenance in PartType.objects.values_list(
            "pk", "recommended_price", "price_provenance"
        )
    }

    # Решение владельца, зафиксированное каноническим сервисом. Только на копии.
    exceptions = set()
    for article in sorted(OWNER_CONFIRMED_EXCEPTIONS):
        part = PartType.objects.filter(numbers__value=article).distinct().get()
        certify_valid_manual_price_exception(part)
        exceptions.add(part.pk)

    plan = plan_linked_part_price_refresh(
        usd_rate=rate, brp_markup=brp_markup, polaris_markup=polaris_markup
    )
    decided = plan.parts_to_update
    stock = availability()
    own, replacement, source_kind = own_wholesale_map()

    public_ids = set(public_parts().values_list("pk", flat=True))
    russian = {
        row[0]: row[1:]
        for row in PartCustomsInfo.objects.values_list(
            "part_type_id",
            "customs_name_ru",
            "customs_name_ru_confirmed",
            "customs_name_source",
            "application_area",
        )
    }

    whole, public, in_stock = Counter(), Counter(), Counter()
    price_changes, review_rows, mismatches = [], [], []

    for pk, (old_price, old_provenance) in before.items():
        obj = decided.get(pk)
        provenance = obj.price_provenance if obj is not None else old_provenance
        new_price = obj.recommended_price if obj is not None else old_price
        price_moves = obj is not None and new_price != old_price
        if pk in exceptions:
            provenance = VALID_MANUAL_EXCEPTION.lower()
        category = provenance.upper()
        if category == FORMULA_CERTIFIED and price_moves:
            category = TRUE_MISMATCH
        if category not in ORDER:
            category = UNVERIFIED
        # Карточка без связи с каталогом поставщика: формулы для неё нет.
        if pk not in source_kind and category in {UNVERIFIED, SOURCE_MISSING}:
            category = NOT_APPLICABLE

        whole[category] += 1
        is_public = pk in public_ids
        quantity = stock.get(pk, ZERO)
        if is_public:
            public[category] += 1
            if quantity > ZERO:
                in_stock[category] += 1
        if price_moves:
            price_changes.append((pk, old_price, new_price))
        if category == TRUE_MISMATCH:
            mismatches.append((pk, old_price, new_price, quantity, is_public))
        if is_public and quantity > ZERO and category not in {FORMULA_CERTIFIED}:
            review_rows.append((pk, category, old_price, new_price, quantity))

    detail = {
        part.pk: part
        for part in with_part_identity(
            PartType.objects.filter(
                pk__in={row[0] for row in review_rows} | {row[0] for row in mismatches}
            ),
            part_field="",
        )
    }

    def row_for(pk, category, old_price, new_price, quantity):
        part = detail[pk]
        article = part_exact_number(part, default="")
        kind, reference, price_source = source_kind.get(pk, ("", "", ""))
        own_usd = own.get(pk)
        chain = replacement.get(pk)
        markup = polaris_markup if kind == "polaris" else brp_markup
        expected = formula_price(own_usd, rate, markup)
        delta = (old_price - expected) if (expected is not None and old_price is not None) else None
        chain_expected = formula_price(chain[1], rate, markup) if chain else None
        matches_chain = (
            chain_expected is not None and old_price is not None and old_price == chain_expected
        )
        ru_name, ru_confirmed = russian.get(pk, ("", False, "", ""))[:2]
        if category == VALID_MANUAL_EXCEPTION:
            reason = "владелец подтвердил отдельную товарную сущность"
            action = CONFIRM_MANUAL
        elif category == TRUE_MISMATCH:
            reason = "своя оптовая цена есть, но текущая цена ей не равна"
            action = HUMAN
        elif category == UNVERIFIED:
            reason = "цена помечена ручной в связи с каталогом: коммерческий смысл не подтверждён"
            if not own_usd and chain:
                reason += (
                    f"; своей оптовой цены нет, у замены {chain[0]} она есть ({chain[1]} $)"
                    + (
                        "; текущая цена РАВНА цене по замене"
                        if matches_chain
                        else f"; по замене вышло бы {chain_expected} руб."
                    )
                )
            action = HUMAN
        elif category == SOURCE_MISSING:
            reason = (
                "у своей позиции каталога нет положительной оптовой цены"
                + (f"; есть замена {chain[0]} ({chain[1]} $), но она не доказывает цену"
                   if chain else "")
            )
            action = NEED_SOURCE if not chain else HUMAN
        else:
            reason = "карточка не связана с каталогом поставщика: формулы для неё нет"
            action = NOT_APPLICABLE_ACTION
        return {
            "article": article,
            "english_name": part.name,
            "russian_name": ru_name,
            "russian_confirmed": "да" if ru_confirmed else "нет",
            "manufacturer": manufacturer_display(part),
            "in_stock_qty": quantity,
            "current_price_rub": old_price if old_price is not None else "",
            "provenance": category,
            "own_wholesale_source": "да" if own_usd else "нет",
            "own_wholesale_usd": own_usd if own_usd else "",
            "replacement_source": "да" if chain else "нет",
            "replacement_article": chain[0] if chain else "",
            "replacement_wholesale_usd": chain[1] if chain else "",
            "formula_price_rub": expected if expected is not None else "",
            "delta_rub": delta if delta is not None else "",
            "replacement_formula_price_rub": chain_expected if chain_expected is not None else "",
            "price_equals_replacement_formula": (
                "да" if matches_chain else ("нет" if chain_expected is not None else "")
            ),
            "catalog": kind,
            "catalog_reference": reference,
            "link_price_source": price_source,
            "why_not_certified": reason,
            "suggested_action": action,
        }

    price_review = [row_for(*row) for row in sorted(review_rows, key=lambda r: -r[4])]
    _write_csv(f"{args.out}/in_stock_price_review.csv", price_review)

    summary = {
        "usd_rate": str(rate),
        "brp_markup": str(brp_markup),
        "polaris_markup": str(polaris_markup),
        "totals": {
            "parts": len(before),
            "public": len(public_ids),
            "in_stock_public": sum(1 for pk in public_ids if stock.get(pk, ZERO) > ZERO),
        },
        "whole": {name: whole.get(name, 0) for name in ORDER},
        "public": {name: public.get(name, 0) for name in ORDER},
        "in_stock": {name: in_stock.get(name, 0) for name in ORDER},
        "in_stock_showable": sum(in_stock.get(name, 0) for name in SHOWABLE),
        "in_stock_clarify": sum(
            in_stock.get(name, 0) for name in ORDER if name not in SHOWABLE
        ),
        "price_changes": len(price_changes),
        "certifiable_without_price_change": whole.get(FORMULA_CERTIFIED, 0),
        "true_mismatches": [
            {
                "part_id": pk,
                "article": part_exact_number(detail[pk], default=""),
                "name": detail[pk].name,
                "current": str(old),
                "formula": str(new),
                "delta": str((old - new) if old is not None and new is not None else ""),
                "in_stock": str(quantity),
                "public": is_public,
            }
            for pk, old, new, quantity, is_public in mismatches
        ],
        "russian": {
            "customs_rows": len(russian),
            "with_name": sum(1 for value in russian.values() if (value[0] or "").strip()),
            "confirmed": sum(
                1 for value in russian.values() if value[1] and (value[0] or "").strip()
            ),
            "in_stock_confirmed": sum(
                1
                for pk, value in russian.items()
                if value[1] and (value[0] or "").strip() and stock.get(pk, ZERO) > ZERO
            ),
        },
    }
    with open(f"{args.out}/phase_b_summary.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _write_csv(path, rows):
    if not rows:
        rows = []
    fields = list(rows[0].keys()) if rows else []
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    print(f"written {path}: {len(rows)} rows")


if __name__ == "__main__":
    main()
