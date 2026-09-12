"""Применить русские названия, которые ПОДТВЕРДИЛ человек в CSV.

Команда ничего не переводит и ничего не решает. Она берёт файл, в котором
оператор уже написал своё решение по каждой группе, и переносит это решение в
таможенные карточки деталей той же дорогой, что и экран правки: через
`get_or_create_customs`, поля `customs_name_ru` / `customs_name_ru_confirmed` /
`customs_name_source` и обычный `save()`, после которого сигнал
`record_version_for_saved_customs` записывает историческую версию. Прямых
UPDATE в обход доменного слоя здесь нет.

По умолчанию это dry-run: он показывает, что именно изменится, и не пишет
ничего. Запись включается явным `--apply`.

Формат файла (UTF-8, разделитель `;`), ровно те колонки, что в шаблоне
`phase_b_ru_approved_template.csv`:

    english_name;approved_russian_name;decision;scope;note

* `decision` только `APPROVE`, `EDITED_APPROVE`, `SKIP`, `REVIEW_LATER`;
  пустая строка и любое другое значение это ошибка файла, а не «пропустить»;
* `scope` это `производитель|каталог` из выгрузки, он же селектор группы:
  одно решение применяется ТОЛЬКО к деталям с тем же английским названием,
  тем же производителем и тем же каталогом-источником. Совпадения одного
  текста названия мало: «BALL BEARING» у разных поставщиков это разные
  позиции, и подтверждать их одним решением нельзя;
* `APPROVE` без `approved_russian_name` не принимается: подсказка из
  review-файла сама по себе подтверждением не является, человек обязан
  вписать текст, под которым подписывается.

Ничего, кроме русского названия и его подтверждения, команда не трогает: ни
цену, ни остаток, ни вес, ни другие таможенные поля.
"""

import csv
from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.actions.models import PartCustomsInfo
from apps.actions.services import get_or_create_customs
from apps.catalog.models import PartType
from apps.inventory.presentation import manufacturer_display, with_part_identity

APPROVE = "APPROVE"
EDITED_APPROVE = "EDITED_APPROVE"
SKIP = "SKIP"
REVIEW_LATER = "REVIEW_LATER"
APPLYING = {APPROVE, EDITED_APPROVE}
DECISIONS = {APPROVE, EDITED_APPROVE, SKIP, REVIEW_LATER}
REQUIRED_COLUMNS = ("english_name", "approved_russian_name", "decision", "scope")
MAX_NAME_LENGTH = 255


@dataclass
class Row:
    line: int
    english_name: str
    approved: str
    decision: str
    scope: str
    note: str = ""


@dataclass
class Outcome:
    planned: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    skipped: int = 0
    protected: list = field(default_factory=list)
    groups_applied: int = 0


def _read_rows(path):
    try:
        handle = open(path, encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise CommandError(f"Не удалось открыть файл решений: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle, delimiter=";")
        missing = [name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or [])]
        if missing:
            raise CommandError(
                "В файле решений нет обязательных колонок: " + ", ".join(missing)
            )
        rows = []
        for number, raw in enumerate(reader, start=2):
            row = Row(
                line=number,
                english_name=(raw.get("english_name") or "").strip(),
                approved=(raw.get("approved_russian_name") or "").strip(),
                decision=(raw.get("decision") or "").strip().upper(),
                scope=(raw.get("scope") or "").strip(),
                note=(raw.get("note") or "").strip(),
            )
            if not row.english_name and not row.decision:
                continue  # пустая строка шаблона
            rows.append(row)
    if not rows:
        raise CommandError("В файле решений нет ни одной строки.")
    return rows


def _validate(rows):
    """Fail closed: любая непонятная строка останавливает всю команду."""
    problems = []
    seen = {}
    for row in rows:
        where = f"строка {row.line}"
        if not row.english_name:
            problems.append(f"{where}: пустое english_name")
            continue
        if row.decision not in DECISIONS:
            problems.append(
                f"{where}: решение {row.decision or 'пустое'!r} не из списка "
                + ", ".join(sorted(DECISIONS))
            )
            continue
        if row.decision in APPLYING and not row.approved:
            problems.append(f"{where}: решение {row.decision} без русского названия")
        if len(row.approved) > MAX_NAME_LENGTH:
            problems.append(f"{where}: название длиннее {MAX_NAME_LENGTH} символов")
        key = (row.english_name, row.scope)
        if key in seen:
            problems.append(
                f"{where}: та же группа уже решена в строке {seen[key]}; "
                "два решения на одну группу принять нельзя"
            )
        seen[key] = row.line
    if problems:
        raise CommandError("Файл решений не принят:\n  " + "\n  ".join(problems))


def _scope_parts(row):
    """Детали группы по её селектору: название + производитель + каталог."""
    from apps.catalog_import.models import AftermarketCatalogPart

    manufacturer, _, catalog = row.scope.partition("|")
    manufacturer, catalog = manufacturer.strip(), catalog.strip()
    queryset = with_part_identity(
        PartType.objects.filter(name=row.english_name), part_field=""
    )
    aftermarket = set(
        AftermarketCatalogPart.objects.filter(part__name=row.english_name).values_list(
            "part_id", flat=True
        )
    )
    selected = []
    for part in queryset:
        if manufacturer and manufacturer_display(part) != manufacturer:
            continue
        if catalog and _catalog_of(part, aftermarket) != catalog:
            continue
        selected.append(part)
    return selected


def _catalog_of(part, aftermarket_ids):
    if getattr(part, "brp_link", None):
        return "BRP"
    if getattr(part, "polaris_link", None):
        return "POLARIS"
    if part.pk in aftermarket_ids:
        return "aftermarket"
    return "нет"


class Command(BaseCommand):
    help = (
        "Перенести подтверждённые человеком русские названия из CSV в таможенные "
        "карточки. По умолчанию dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Файл решений оператора.")
        parser.add_argument(
            "--apply", action="store_true", help="Записать изменения (иначе dry-run)."
        )
        parser.add_argument(
            "--allow-replace",
            action="store_true",
            help="Разрешить менять уже подтверждённое русское название.",
        )
        parser.add_argument(
            "--user",
            help="Логин сотрудника, от чьего имени записывается решение (для истории).",
        )

    def handle(self, *args, **options):
        rows = _read_rows(options["csv_path"])
        _validate(rows)
        author = self._author(options.get("user"))
        outcome = Outcome()

        write = self.stdout.write
        write("Применение подтверждённых русских названий")
        write("Режим: ЗАПИСЬ" if options["apply"] else "Режим: DRY-RUN (ничего не пишется)")
        write("")

        for row in rows:
            if row.decision not in APPLYING:
                outcome.skipped += 1
                continue
            parts = _scope_parts(row)
            if not parts:
                raise CommandError(
                    f"строка {row.line}: под селектор «{row.english_name}» / «{row.scope}» "
                    "не попала ни одна деталь. Файл решений устарел, применение остановлено."
                )
            group_changes = 0
            for part in parts:
                customs = PartCustomsInfo.objects.filter(part_type=part).first()
                current = (customs.customs_name_ru if customs else "") or ""
                confirmed = bool(customs and customs.customs_name_ru_confirmed)
                if confirmed and current.strip() and current != row.approved:
                    if not options["allow_replace"]:
                        outcome.protected.append((row, part, current))
                        continue
                if current == row.approved and confirmed:
                    outcome.unchanged.append((row, part))
                    continue
                outcome.planned.append((row, part, current))
                group_changes += 1
            if group_changes:
                outcome.groups_applied += 1

        self._report(outcome, rows)
        if not options["apply"]:
            write("")
            write("Dry-run: карточки, цены, остатки и история не изменялись.")
            return

        with transaction.atomic():
            for row, part, _before in outcome.planned:
                customs = get_or_create_customs(part)
                customs.customs_name_ru = row.approved
                customs.customs_name_ru_confirmed = True
                customs.customs_name_source = PartCustomsInfo.NameSource.MANUAL
                customs.updated_by = author
                customs.save(
                    update_fields=[
                        "customs_name_ru",
                        "customs_name_ru_confirmed",
                        "customs_name_source",
                        "updated_by",
                        "updated_at",
                    ]
                )
        write("")
        write(
            self.style.SUCCESS(
                f"Записано названий: {len(outcome.planned)} "
                f"в {outcome.groups_applied} группах."
            )
        )

    def _author(self, username):
        if not username:
            return None
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"Пользователь {username!r} не найден.")
        return user

    def _report(self, outcome, rows):
        write = self.stdout.write
        write(f"Строк решений в файле: {len(rows)}")
        write(f"  к записи: {len(outcome.planned)} названий в {outcome.groups_applied} группах")
        write(f"  уже стоят и подтверждены: {len(outcome.unchanged)}")
        write(f"  пропущено решением человека (SKIP / REVIEW_LATER): {outcome.skipped}")
        write(f"  защищено от замены (уже подтверждено другое): {len(outcome.protected)}")
        if outcome.protected:
            write("")
            write("Не тронуто, потому что название уже подтверждено другим текстом:")
            for row, part, current in outcome.protected[:20]:
                write(f"  {part.pk} {row.english_name}: «{current}» -> «{row.approved}»")
            write("  (чтобы всё же заменить, нужен --allow-replace)")
        if outcome.planned:
            write("")
            write("Что изменится:")
            for row, part, before in outcome.planned[:40]:
                was = f"«{before}»" if before else "пусто"
                write(f"  {part.pk} {row.english_name}: {was} -> «{row.approved}» (подтверждено)")
            if len(outcome.planned) > 40:
                write(f"  ... и ещё {len(outcome.planned) - 40}")
