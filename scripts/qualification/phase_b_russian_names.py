"""Phase B: Russian-name readiness for in-stock parts + translation suggestions.

Read-only against an isolated clone. Writes two CSV review files. Suggestions
are DRAFTS for an operator: nothing is written to the database and nothing is
marked confirmed.
"""
import argparse
import csv
import json
import re
from collections import Counter
from decimal import Decimal

import django

django.setup()

from apps.actions.models import PartCustomsInfo  # noqa: E402
from apps.catalog.models import PartType  # noqa: E402
from apps.catalog.public_catalog import public_parts  # noqa: E402
from apps.catalog.public_contracts import resolve_current_customer_price  # noqa: E402
from apps.catalog_import.models import AftermarketCatalogPart  # noqa: E402
from apps.inventory.movement import live_stock_rows  # noqa: E402
from apps.inventory.presentation import (  # noqa: E402
    manufacturer_display,
    part_exact_number,
    with_part_identity,
)

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
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
CODE = re.compile(r"^[A-Z]?\d|^M\d|^\d")

# Головные существительные. Первое найденное задаёт название по-русски.
HEADS = {
    "O-RING": "Кольцо уплотнительное",
    "SPARK PLUG": "Свеча зажигания",
    "BALL JOINT": "Шаровая опора",
    "TIE ROD END": "Наконечник рулевой тяги",
    "CONNECTING ROD": "Шатун",
    "CV JOINT": "ШРУС",
    "UNIVERSAL JOINT": "Крестовина",
    "BALL BEARING": "Подшипник шариковый",
    "AIR FILTER": "Фильтр воздушный",
    "OIL FILTER": "Фильтр масляный",
    "FUEL FILTER": "Фильтр топливный",
    "SEAL": "Сальник",
    "GASKET": "Прокладка",
    "BEARING": "Подшипник",
    "BUSHING": "Втулка",
    "PISTON": "Поршень",
    "SCREW": "Винт",
    "BOLT": "Болт",
    "NUT": "Гайка",
    "WASHER": "Шайба",
    "SHIM": "Шайба регулировочная",
    "SPRING": "Пружина",
    "VALVE": "Клапан",
    "CLAMP": "Хомут",
    "BOOT": "Пыльник",
    "SWITCH": "Выключатель",
    "FILTER": "Фильтр",
    "LATCH": "Защёлка",
    "STOPPER": "Упор",
    "COVER": "Крышка",
    "SUPPORT": "Кронштейн",
    "BRACKET": "Кронштейн",
    "GRIP": "Рукоятка",
    "CYLINDER": "Цилиндр",
    "HOUSING": "Корпус",
    "CIRCLIP": "Кольцо стопорное",
    "SNAP RING": "Кольцо стопорное",
    "PIN": "Палец",
    "CAP": "Колпачок",
    "CUSHION": "Подушка",
    "CHAIN": "Цепь",
    "PADS": "Колодки",
    "PAD": "Колодка",
    "HANDLE": "Ручка",
    "PROTECTOR": "Защита",
    "PUMP": "Насос",
    "PLATE": "Пластина",
    "CABLE": "Трос",
    "BELT": "Ремень",
    "IMPELLER": "Крыльчатка",
    "ADAPTOR": "Переходник",
    "ADAPTER": "Переходник",
    "REFLECTOR": "Катафот",
    "BUMPER": "Бампер",
    "GRILL": "Решётка",
    "HOSE": "Шланг",
    "TUBE": "Трубка",
    "WHEEL": "Колесо",
    "SPROCKET": "Звёздочка",
    "PULLEY": "Шкив",
    "TENSIONER": "Натяжитель",
    "ROLLER": "Ролик",
    "GUIDE": "Направляющая",
    "SLIDER": "Ползун",
    "RETAINER": "Фиксатор",
    "SPACER": "Проставка",
    "GROMMET": "Втулка резиновая",
    "RIVET": "Заклёпка",
    "CLIP": "Клипса",
    "HINGE": "Петля",
    "MIRROR": "Зеркало",
    "LAMP": "Лампа",
    "SENSOR": "Датчик",
    "THERMOSTAT": "Термостат",
    "RADIATOR": "Радиатор",
    "MUFFLER": "Глушитель",
    "MANIFOLD": "Коллектор",
    "CARBURETOR": "Карбюратор",
    "INJECTOR": "Форсунка",
    "STARTER": "Стартер",
    "BATTERY": "Аккумулятор",
    "SEAT": "Сиденье",
    "WINDSHIELD": "Ветровое стекло",
    "SKI": "Лыжа",
    "BELLOW": "Гофра",
    "BELLOWS": "Гофра",
    "NEEDLE": "Игла",
    "GEAR": "Шестерня",
    "SHAFT": "Вал",
    "AXLE": "Ось",
    "LEVER": "Рычаг",
    "DAMPER": "Демпфер",
    "DECAL": "Наклейка",
    "STICKER": "Наклейка",
    "PLUG": "Пробка",
    "NIPPLE": "Ниппель",
    "FITTING": "Штуцер",
    "FLANGE": "Фланец",
    "KEY": "Ключ",
    "GUARD": "Защита",
    "HOOD": "Капот",
    "BUTTON": "Кнопка",
    "CUP": "Стакан",
    "FORK": "Вилка",
    "IGNITION COIL": "Катушка зажигания",
    "COIL": "Катушка",
    "MODULE": "Модуль",
    "DEFLECTOR": "Дефлектор",
    "INSERT": "Вставка",
    "ISOLATOR": "Изолятор",
    "GAUGE": "Указатель",
    "RAIL": "Рампа",
    "BRUSH": "Щётка",
    "WEIGHT": "Груз",
    "CORD": "Шнур",
    "POST": "Стойка",
    "VENT": "Вентиляционная решётка",
    "NECK": "Горловина",
    "SHEAVE": "Щека шкива",
    "SKID": "Отбойник",
    "MEMBER": "Балка",
    "BOX": "Коробка",
    "ELEMENT": "Элемент",
    "FILM": "Плёнка",
    "HANDLEBAR": "Руль",
    "CUTTER": "Резак",
    "FASTENER": "Крепёж",
    "SLEEVE": "Втулка распорная",
    "TETHER": "Чека",
    "STUD": "Шпилька",
    "SPACER RING": "Кольцо проставочное",
    "OIL PUMP": "Насос масляный",
    "WATER PUMP": "Насос водяной",
    "FUEL PUMP": "Насос топливный",
    "BILGE PUMP": "Насос трюмный",
    "MUD FLAP": "Брызговик",
    "FOOTREST": "Подножка",
    "SILENT BLOCK": "Сайлентблок",
    "TRACK": "Гусеница",
    "WINDOW": "Стекло",
    "LENS": "Рассеиватель",
    "HARNESS": "Жгут проводов",
    "CONNECTOR": "Разъём",
    "RELAY": "Реле",
    "FUSE": "Предохранитель",
    "SOLENOID": "Соленоид",
    "MOUNT": "Опора",
    "STRAP": "Ремешок",
    "HOOK": "Крюк",
    "PANEL": "Панель",
    "SHOE": "Ползун",
    "RUNNER": "Направляющая",
    "SOCKET": "Гнездо",
    "MESH": "Сетка",
    "PRESSURE PLATE": "Нажимной диск",
    "PISTON PIN": "Палец поршневой",
    "PISTON RING": "Кольцо поршневое",
    "PISTON CIRCLIP": "Кольцо стопорное поршневого пальца",
    "NEEDLE BEARING": "Подшипник игольчатый",
    "BEARING NEEDLE": "Подшипник игольчатый",
    "BEARING HOUSING": "Корпус подшипника",
    "BEARING SLEEVE": "Втулка подшипника",
    "SLIDER SHOE": "Ползун направляющей",
    "ROLLER PULLEY": "Ролик шкива",
    "PULLEY ROLLER": "Ролик шкива",
    "OIL SEAL": "Сальник",
    "VALVE SPRING": "Пружина клапана",
    "BRAKE PADS": "Колодки тормозные",
    "BRAKE PAD": "Колодка тормозная",
    "BRAKE LINE": "Магистраль тормозная",
    "BRAKE SWITCH": "Выключатель стоп-сигнала",
    "AIR INTAKE FILTER": "Фильтр воздухозаборника",
    "DRIVE BELT": "Ремень вариатора",
    "BELT DRIVE": "Ремень вариатора",
    "CHAIN TENSIONER": "Натяжитель цепи",
    "SPARK PLUG CAP": "Колпачок свечи зажигания",
    "GEAR SHIFT": "Переключение передач",
    "WHEEL CAP": "Колпак колеса",
    "HANDLE GRIP": "Ручка руля",
    "GRIP HEATER": "Подогрев рукояток",
    "FUEL RAIL": "Рампа топливная",
    "DRAIN PLUG": "Пробка сливная",
    "SHIFT FORK": "Вилка переключения",
    "TIE ROD": "Тяга рулевая",
    "SWAY BAR BUSHING": "Втулка стабилизатора",
    "IMPELLER BOOT": "Пыльник крыльчатки",
    "CV JOINT BOOT KIT": "Комплект пыльника ШРУС",
    "BOOT CV JOINT KIT": "Комплект пыльника ШРУС",
    "TOP END GASKET SET": "Комплект прокладок верхней части двигателя",
    "OIL PRESSURE SWITCH": "Датчик давления масла",
    "CHECK VALVE": "Клапан обратный",
    "SKI STOPPER": "Ограничитель лыжи",
    "EXHAUST VALVE": "Клапан выпускной",
    "TIE ROD END KIT": "Комплект наконечников рулевой тяги",
    "SHOCK ABSORBER": "Амортизатор",
    "WHEEL BEARING": "Подшипник ступицы",
    "CRANKSHAFT SEAL": "Сальник коленвала",
    "VALVE COVER GASKET": "Прокладка крышки клапанов",
    "CYLINDER HEAD GASKET": "Прокладка головки блока цилиндров",
    "EXHAUST GASKET": "Прокладка выпускного коллектора",
    "INTAKE GASKET": "Прокладка впускного коллектора",
}
# Слова, которые сами по себе значат слишком много: без человека не решить.
AMBIGUOUS_HEADS = {
    "ROD", "WEB", "SET", "END", "PAN", "LINE", "BLOCK", "HALF", "ARM", "BAR",
    "CAM", "JOINT", "RING", "KIT", "ASSY", "ASS'Y", "BODY", "HEAD", "BASE",
}
MODIFIERS = {
    "OIL": "масляный", "AIR": "воздушный", "FUEL": "топливный",
    "WATER": "водяной", "BRAKE": "тормозной", "DRIVE": "приводной",
    "EXHAUST": "выпускной", "INTAKE": "впускной", "FRONT": "передний",
    "REAR": "задний", "LEFT": "левый", "LH": "левый", "RIGHT": "правый",
    "RH": "правый", "UPPER": "верхний", "LOWER": "нижний",
    "INNER": "внутренний", "OUTER": "наружный", "RUBBER": "резиновый",
    "METAL": "металлический", "PLASTIC": "пластиковый", "BLACK": "чёрный",
    "HEX": "шестигранный", "HEX.": "шестигранный", "FLANGED": "фланцевый",
    "UNIVERSAL": "универсальный", "ANGLED": "угловой", "LONG": "длинный",
    "SHORT": "короткий", "COMPLETE": "в сборе", "KIT": "комплект",
    "ASSY": "в сборе", "ASS'Y": "в сборе", "STEERING": "рулевой",
    "SUSPENSION": "подвески", "STABILIZER": "стабилизатора",
    "CRANKCASE": "картера", "CRANKSHAFT": "коленвала", "ENGINE": "двигателя",
}
BRANDS = {
    "BRONCO", "SPI", "PROX", "WILDBOAR", "NGK", "OETIKER", "SKI-DOO", "SEA-DOO",
    "ALL", "BALLS", "WISECO", "POLARIS", "BRP", "YUASA",
    "KENDA", "PIRELLI", "BRIDGESTONE", "CONTINENTAL", "EBC", "FMF", "POLISPORT",
    "PSYCHIC", "LIQUI", "MOLY", "K100", "SPX", "NITEX",
}
SIDES = {"LH": "левый", "RH": "правый", "LEFT": "левый", "RIGHT": "правый"}
# Термин переводится, но у него в каталоге запчастей есть несколько равноправных
# вариантов, и выбрать может только человек: «CAP» это и колпачок, и крышка, и
# колпак. Такие названия попадают в MEDIUM, а не в HIGH.
CHECK_MEANING = {"CAP", "PLATE", "SUPPORT", "INSERT", "ELEMENT", "MODULE", "BOX", "SHOE"}


def tokenize(name):
    return [token for token in re.split(r"[\s,()/_]+", name.upper().replace("-", " ")) if token]


def suggest(name):
    """Черновик русского названия, уверенность, причина и съеденные термином слова.

    Последнее нужно проверке многозначности: в «O-RING» слово RING само по себе
    многозначное, но внутри термина «O-RING» никакой многозначности нет.
    """
    if CYRILLIC.search(name):
        return "", "NEEDS HUMAN REVIEW", "название уже содержит русский текст", set()
    tokens = tokenize(name)
    if not tokens:
        return "", "NEEDS HUMAN REVIEW", "пустое название", set()

    upper = " ".join(tokens)
    # Английские составные названия головные справа: «PISTON PIN» это палец, а
    # не поршень. Поэтому при равной длине выигрывает ПОСЛЕДНЕЕ вхождение, а
    # более длинная фраза всегда сильнее короткой («AIR FILTER» важнее
    # «FILTER»).
    best = None
    for phrase in HEADS:
        needle = phrase.replace("-", " ")
        match = None
        for found in re.finditer(rf"(^| ){re.escape(needle)}( |$)", upper):
            match = found
        if match is None:
            continue
        key = (len(needle.split()), match.start())
        if best is None or key > best[0]:
            best = (key, phrase, needle)
    head = HEADS[best[1]] if best else None
    if head is None:
        first = tokens[0]
        if first in AMBIGUOUS_HEADS:
            return (
                "",
                "NEEDS HUMAN REVIEW",
                f"слово «{first}» без человека однозначно не перевести",
                set(),
            )
        return "", "NEEDS HUMAN REVIEW", "головное слово не распознано", set()

    used = set(best[2].split())
    tail, unknown, notes, secondary = [], [], [], []
    for token in tokens:
        if token in used:
            continue
        if token in BRANDS:
            notes.append(f"бренд {token} оставлен как есть")
            tail.append(token)
            continue
        if token in SIDES:
            tail.append(SIDES[token])
            continue
        if token in MODIFIERS:
            tail.append(MODIFIERS[token])
            continue
        if token in HEADS:
            # Знакомый термин, но не головной: слово переводим, а порядок слов
            # в русском названии пусть проверит человек.
            tail.append(HEADS[token].lower())
            secondary.append(token)
            continue
        if CODE.match(token) or len(token) <= 2:
            tail.append(token)
            continue
        unknown.append(token)
        tail.append(token)

    suggestion = " ".join([head, *tail]).strip()
    if unknown:
        return (
            suggestion,
            "NEEDS HUMAN REVIEW",
            "не переведены слова: " + ", ".join(sorted(set(unknown))),
            used,
        )
    if any(token in CHECK_MEANING for token in tokens):
        checked = sorted({token for token in tokens if token in CHECK_MEANING})
        return (
            suggestion,
            "MEDIUM",
            "у слова несколько принятых переводов, выберите нужный: " + ", ".join(checked),
            used,
        )
    if secondary:
        return (
            suggestion,
            "MEDIUM",
            "проверьте порядок слов: " + ", ".join(sorted(set(secondary))),
            used,
        )
    if any(token in AMBIGUOUS_HEADS for token in tokens if token not in used):
        return suggestion, "MEDIUM", "в названии есть многозначное слово, проверьте смысл", used
    return suggestion, "HIGH", "", used


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--confirm-isolated", action="store_true")
    parser.add_argument("--expect-database", required=True)
    args = parser.parse_args()
    guard(args, writes=False)

    stock: Counter = Counter()
    for row in live_stock_rows():
        stock[row.part_type.pk] += row.available
    in_stock = {pid: qty for pid, qty in stock.items() if qty > ZERO}

    customs = {
        row[0]: row[1:]
        for row in PartCustomsInfo.objects.values_list(
            "part_type_id",
            "customs_name_ru",
            "customs_name_ru_confirmed",
            "customs_name_source",
            "application_area",
        )
    }
    aftermarket = set(AftermarketCatalogPart.objects.values_list("part_id", flat=True))
    public_ids = set(public_parts().values_list("pk", flat=True))
    parts = {
        part.pk: part
        for part in with_part_identity(
            PartType.objects.filter(pk__in=in_stock), part_field=""
        ).select_related("customs_info")
    }

    review, suggestions, special = [], [], Counter()
    confidence = Counter()
    for pk, quantity in sorted(in_stock.items(), key=lambda item: -item[1]):
        part = parts.get(pk)
        if part is None or pk not in public_ids:
            continue
        article = part_exact_number(part, default="")
        maker = manufacturer_display(part)
        name_ru, confirmed, source, application = customs.get(pk, ("", False, "", ""))
        price = resolve_current_customer_price(part)
        catalog = (
            "BRP"
            if getattr(part, "brp_link", None)
            else "POLARIS"
            if getattr(part, "polaris_link", None)
            else "aftermarket"
            if pk in aftermarket
            else "нет"
        )
        flags = []
        if not article:
            flags.append("без артикула")
            special["без артикула"] += 1
        if not maker:
            flags.append("без производителя")
            special["без производителя"] += 1
        if catalog == "нет":
            flags.append("нет каталога поставщика")
            special["нет каталога поставщика"] += 1
        if CYRILLIC.search(part.name):
            flags.append("название уже по-русски")
            special["название уже по-русски"] += 1
        if "REBUILD" in part.name.upper():
            flags.append("rebuild")
            special["rebuild"] += 1
        review.append(
            {
                "article": article,
                "english_name": part.name,
                "manufacturer": maker,
                "in_stock_qty": quantity,
                "application_area": application,
                "current_russian_name": name_ru,
                "russian_confirmed": "да" if confirmed else "нет",
                "russian_source": source,
                "catalog": catalog,
                "public_price_status": (
                    "показывается" if price.status == "known" else "Уточнить цену"
                ),
                "public_price_rub": price.price_rub if price.price_rub is not None else "",
                "special": ", ".join(flags),
            }
        )
        text, level, note, _used = suggest(part.name)
        confidence[level] += 1
        suggestions.append(
            {
                "article": article,
                "english_name": part.name,
                "suggested_russian_name": text,
                "confidence": level,
                "ambiguity_note": note,
                "manufacturer": maker,
                "in_stock_qty": quantity,
                "priority": 1 if maker == "BRP" else 2,
                "special": ", ".join(flags),
            }
        )

    suggestions.sort(key=lambda row: (row["priority"], -row["in_stock_qty"]))
    _write(f"{args.out}/in_stock_russian_name_review.csv", review)
    _write(f"{args.out}/ru_translation_suggestions.csv", suggestions)

    groups = build_groups(review)
    repeated = [row for row in groups if row["number_of_in_stock_parttypes"] > 1]
    _write(f"{args.out}/phase_b_ru_unique_review.csv", groups)
    _write(f"{args.out}/phase_b_ru_top104_review.csv", repeated)
    write_approval_template(f"{args.out}/phase_b_ru_approved_template.csv", repeated)

    # Русский текст в самом названии карточки: смотрим ВСЕ публичные карточки,
    # не только те, что есть на складе. Такой текст сам по себе подтверждением
    # не является, но это самые быстрые кандидаты на ручное подтверждение.
    already_russian = []
    for part in with_part_identity(
        PartType.objects.filter(pk__in=public_ids, name__regex=r"[А-Яа-яЁё]"), part_field=""
    ):
        name_ru, confirmed, source, _application = customs.get(part.pk, ("", False, "", ""))
        quantity = in_stock.get(part.pk, ZERO)
        already_russian.append(
            {
                "article": part_exact_number(part, default=""),
                "parttype_name": part.name,
                "customs_source": source or "нет таможенной карточки",
                "current_customs_name_ru": name_ru,
                "russian_confirmed": "да" if confirmed else "нет",
                "suggested_canonical_ru": part.name.strip(),
                "manufacturer": manufacturer_display(part),
                "in_stock": "да" if quantity > ZERO else "нет",
                "in_stock_qty": quantity,
                "recommended_action": (
                    "подтвердить как есть, если название верное; иначе исправить"
                ),
            }
        )
    already_russian.sort(key=lambda row: (row["in_stock"] != "да", -row["in_stock_qty"]))
    _write(f"{args.out}/phase_b_existing_russian_text_review.csv", already_russian)
    safe = [row for row in groups if row["safe_bulk_group"] == "да"]
    safe_repeated = [row for row in repeated if row["safe_bulk_group"] == "да"]
    summary = {
        "unique_groups": len(groups),
        "repeated_groups": len(repeated),
        "repeated_groups_cover_parts": sum(
            row["number_of_in_stock_parttypes"] for row in repeated
        ),
        "repeated_by_confidence": dict(Counter(row["confidence"] for row in repeated)),
        "safe_bulk_groups": len(safe),
        "safe_bulk_groups_cover_parts": sum(
            row["number_of_in_stock_parttypes"] for row in safe
        ),
        "safe_repeated_groups": len(safe_repeated),
        "safe_repeated_cover_parts": sum(
            row["number_of_in_stock_parttypes"] for row in safe_repeated
        ),
        "multi_meaning_groups": sum(1 for row in groups if row["multi_meaning"] == "да"),
        "existing_russian_text_rows": len(already_russian),
        "in_stock_public": len(review),
        "with_russian_name": sum(1 for row in review if row["current_russian_name"].strip()),
        "confirmed_russian": sum(1 for row in review if row["russian_confirmed"] == "да"),
        "suggestions": len(suggestions),
        "confidence": dict(confidence),
        "special_categories": dict(special),
        "by_catalog": dict(Counter(row["catalog"] for row in review)),
        "price_status": dict(Counter(row["public_price_status"] for row in review)),
    }
    with open(f"{args.out}/phase_b_russian_summary.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _write(path, rows):
    fields = list(rows[0].keys()) if rows else []
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    print(f"written {path}: {len(rows)} rows")



# --- Группы для одобрения владельцем ---------------------------------------------------


# Селектор группы. Одно человеческое решение применяется ко ВСЕМ карточкам
# группы, поэтому группа обязана быть однородной. Одного совпадения текста
# английского названия мало: «BALL BEARING» у BRP и «BALL BEARING» у стороннего
# поставщика это разные позиции разных каталогов, и подтверждать их одним
# решением нельзя. Селектор берёт производителя и каталог-источник вместе с
# точным названием - это самый узкий набор, который остаётся стабильным между
# выгрузкой на проверку и применением.
GROUP_FIELDS = ("english_name", "manufacturer", "catalog")


def group_key(row):
    return (row["english_name"], row["manufacturer"], row["catalog"])


def catalog_wide_counts(rows):
    """Сколько ВСЕГО карточек попадёт под каждую группу, а не только в наличии.

    Решение оператора про название относится к названию, а не к сегодняшнему
    остатку: «OIL SEAL» без остатка это тот же сальник. Поэтому подтверждение
    применится и к карточкам без остатка, и человек должен видеть настоящий
    масштаб до того, как подпишется.
    """
    from apps.catalog_import.models import AftermarketCatalogPart

    names = {row["english_name"] for row in rows}
    aftermarket = set(
        AftermarketCatalogPart.objects.filter(part__name__in=names).values_list(
            "part_id", flat=True
        )
    )
    counts: Counter = Counter()
    for part in with_part_identity(PartType.objects.filter(name__in=names), part_field=""):
        catalog = (
            "BRP"
            if getattr(part, "brp_link", None)
            else "POLARIS"
            if getattr(part, "polaris_link", None)
            else "aftermarket"
            if part.pk in aftermarket
            else "нет"
        )
        counts[(part.name, manufacturer_display(part), catalog)] += 1
    return counts


def build_groups(rows):
    """Свернуть построчный список в группы «одно решение = один перевод»."""
    groups = {}
    for row in rows:
        key = group_key(row)
        group = groups.setdefault(
            key,
            {
                "english_name": row["english_name"],
                "manufacturer": row["manufacturer"],
                "catalog": row["catalog"],
                "parts": [],
            },
        )
        group["parts"].append(row)
    by_name = Counter(row["english_name"] for row in rows)
    name_groups = Counter(key[0] for key in groups)
    catalog_wide = catalog_wide_counts(rows)
    result = []
    for group in groups.values():
        parts = group["parts"]
        suggestion, confidence, note, consumed = suggest(group["english_name"])
        tokens = set(tokenize(group["english_name"]))
        reasons = []
        if note:
            reasons.append(note)
        multi = sorted((tokens - consumed) & AMBIGUOUS_HEADS)
        if multi:
            reasons.append(
                "многозначные слова, одним переводом на всю базу не закрывается: "
                + ", ".join(multi)
            )
        split = name_groups[group["english_name"]] > 1
        if split:
            reasons.append(
                "это же название встречается у другого производителя или каталога: "
                "группа разделена, решение действует только на свою"
            )
        russian_in_name = any(CYRILLIC.search(part["english_name"]) for part in parts)
        if russian_in_name:
            reasons.append("в самом названии карточки уже есть русский текст")
        # Разделение по производителю сужает область действия решения, но
        # смысл названия от этого не становится многозначным: «BALL BEARING»
        # у любого поставщика остаётся шариковым подшипником. Поэтому сплит
        # безопасности не отменяет, а многозначное слово - отменяет.
        safe = bool(suggestion and confidence == "HIGH" and not multi and not russian_in_name)
        decision = "APPROVE" if safe else ("EDIT" if suggestion else "REVIEW")
        applications = sorted(
            {part["application_area"] for part in parts if part["application_area"]}
        )
        result.append(
            {
                "english_name": group["english_name"],
                "suggested_russian_name": suggestion,
                "confidence": confidence.replace(" ", "_"),
                "ambiguity_reason": "; ".join(reasons),
                "number_of_in_stock_parttypes": len(parts),
                "total_in_stock_qty": sum(Decimal(str(part["in_stock_qty"])) for part in parts),
                "manufacturers": group["manufacturer"] or "",
                "catalog": group["catalog"],
                "sample_articles": ", ".join(
                    [part["article"] for part in parts if part["article"]][:5]
                ),
                "applications": ", ".join(applications),
                "name_contains_russian": "да" if russian_in_name else "нет",
                "multi_meaning": "да" if multi else "нет",
                "split_by_source": "да" if split else "нет",
                "safe_bulk_group": "да" if safe else "нет",
                "parts_with_this_english_name_total": by_name[group["english_name"]],
                "parttypes_in_whole_catalog": catalog_wide.get(
                    (group["english_name"], group["manufacturer"], group["catalog"]),
                    len(parts),
                ),
                "suggested_operator_decision": decision,
            }
        )
    order = {"HIGH": 0, "MEDIUM": 1, "NEEDS_HUMAN_REVIEW": 2}
    result.sort(
        key=lambda row: (
            -row["number_of_in_stock_parttypes"],
            -row["total_in_stock_qty"],
            order.get(row["confidence"], 3),
            row["english_name"],
        )
    )
    return result


APPROVAL_FIELDS = ("english_name", "approved_russian_name", "decision", "scope", "note")


def write_approval_template(path, groups):
    """Пустой бланк решения. Ни одна подсказка не считается одобрением."""
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(APPROVAL_FIELDS)
        for group in groups:
            writer.writerow(
                [
                    group["english_name"],
                    "",  # оператор вписывает сам; подсказка лежит в review-файле
                    "",  # APPROVE / EDITED_APPROVE / SKIP / REVIEW_LATER
                    f"{group['manufacturers']}|{group['catalog']}",
                    "",
                ]
            )
    print(f"written {path}: {len(groups)} rows")

if __name__ == "__main__":
    main()
