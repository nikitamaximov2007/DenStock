"""Перенос подтверждённых человеком русских названий из CSV в карточки.

Что здесь гарантируется:

* без `--apply` не пишется ничего;
* пишется только то, под чем человек подписался: APPROVE и EDITED_APPROVE;
  SKIP и REVIEW_LATER не трогают базу, а непонятное решение и кривой файл
  останавливают команду целиком (fail closed);
* одно решение применяется ровно к своей группе: название + производитель +
  каталог. Совпадения одного текста названия недостаточно;
* уже подтверждённое название не переписывается без явного `--allow-replace`;
* повторный запуск ничего не меняет второй раз;
* цена, остаток и остальные таможенные поля не меняются;
* подтверждённое название сразу находится русским поиском - и публичным, и
  операторским.
"""

import csv
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from apps.actions.models import PartCustomsDataVersion, PartCustomsInfo
from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.catalog.public_contracts import build_public_part_facts
from apps.catalog.search import search_part_ids
from apps.core.part_lookup import resolve_part_lookup

HEADER = ("english_name", "approved_russian_name", "decision", "scope", "note")


@pytest.fixture
def catalog(db):
    class Builder:
        def __init__(self):
            self.category = Category.objects.create(name="Подтверждение названий")
            self.unit = Unit.objects.get(name="Штука")

        def part(self, name, *, article, maker="BRP", price="1000"):
            manufacturer = (
                Manufacturer.objects.get_or_create(name=maker)[0] if maker else None
            )
            part = PartType.objects.create(
                name=name,
                category=self.category,
                manufacturer=manufacturer,
                unit=self.unit,
                tracking_mode=PartType.TrackingMode.BULK,
                recommended_price=Decimal(price) if price is not None else None,
            )
            PartNumber.objects.create(
                part=part, value=article, kind=PartNumber.Kind.OEM, is_primary=True
            )
            return part

        def brp(self, part, material_no):
            catalog_part = BrpCatalogPart.objects.create(
                material_no=material_no,
                part_desc=part.name,
                wholesale_price_usd=Decimal("10"),
                is_current=True,
            )
            BrpPartLink.objects.create(
                part=part,
                brp_part=catalog_part,
                brp_retail_price_usd=Decimal("0"),
                brp_wholesale_price_usd=Decimal("10"),
                usd_rate_used=Decimal("105"),
                markup_percent_used=Decimal("40"),
            )
            return catalog_part

    return Builder()


def write_csv(tmp_path, rows, *, header=HEADER, name="approved.csv"):
    path = tmp_path / name
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def russian_of(part):
    info = PartCustomsInfo.objects.filter(part_type=part).first()
    return (info.customs_name_ru, info.customs_name_ru_confirmed) if info else ("", False)


# --- Dry-run --------------------------------------------------------------------------


def test_dry_run_writes_nothing(catalog, tmp_path):
    part = catalog.part("O-RING", article="420631610")
    path = write_csv(tmp_path, [("O-RING", "Кольцо уплотнительное", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path)

    assert PartCustomsInfo.objects.count() == 0
    assert russian_of(part) == ("", False)


def test_apply_writes_the_approved_name(catalog, tmp_path):
    part = catalog.part("O-RING", article="420631610")
    path = write_csv(tmp_path, [("O-RING", "Кольцо уплотнительное", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(part) == ("Кольцо уплотнительное", True)
    info = PartCustomsInfo.objects.get(part_type=part)
    assert info.customs_name_source == PartCustomsInfo.NameSource.MANUAL


def test_edited_approve_writes_the_edited_text(catalog, tmp_path):
    part = catalog.part("SEAL", article="S-1")
    path = write_csv(tmp_path, [("SEAL", "Сальник коленвала", "EDITED_APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(part) == ("Сальник коленвала", True)


@pytest.mark.parametrize("decision", ["SKIP", "REVIEW_LATER"])
def test_a_postponed_decision_writes_nothing(catalog, tmp_path, decision):
    part = catalog.part("BUSHING", article="B-1")
    path = write_csv(tmp_path, [("BUSHING", "Втулка", decision, "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(part) == ("", False)


# --- Fail closed ----------------------------------------------------------------------


@pytest.mark.parametrize("decision", ["", "ok", "APPLY", "да", "approve!"])
def test_an_unknown_decision_stops_everything(catalog, tmp_path, decision):
    good = catalog.part("GASKET", article="G-1")
    bad = catalog.part("BUSHING", article="B-2")
    path = write_csv(
        tmp_path,
        [
            ("GASKET", "Прокладка", "APPROVE", "BRP|нет", ""),
            ("BUSHING", "Втулка", decision, "BRP|нет", ""),
        ],
    )

    with pytest.raises(CommandError, match="Файл решений не принят"):
        call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(good) == ("", False), "хорошая строка тоже не применяется"
    assert russian_of(bad) == ("", False)


def test_approve_without_a_russian_name_is_refused(catalog, tmp_path):
    catalog.part("GASKET", article="G-2")
    path = write_csv(tmp_path, [("GASKET", "", "APPROVE", "BRP|нет", "")])

    with pytest.raises(CommandError, match="без русского названия"):
        call_command("apply_confirmed_russian_names", path, "--apply")


def test_two_decisions_for_one_group_are_refused(catalog, tmp_path):
    catalog.part("GASKET", article="G-3")
    path = write_csv(
        tmp_path,
        [
            ("GASKET", "Прокладка", "APPROVE", "BRP|нет", ""),
            ("GASKET", "Уплотнение", "APPROVE", "BRP|нет", ""),
        ],
    )

    with pytest.raises(CommandError, match="уже решена в строке"):
        call_command("apply_confirmed_russian_names", path, "--apply")


def test_a_file_without_the_required_columns_is_refused(catalog, tmp_path):
    path = write_csv(
        tmp_path, [("GASKET", "Прокладка")], header=("english_name", "approved_russian_name")
    )

    with pytest.raises(CommandError, match="нет обязательных колонок"):
        call_command("apply_confirmed_russian_names", path)


def test_a_stale_selector_stops_the_run(catalog, tmp_path):
    """Название переименовали после выгрузки: применять вслепую нельзя."""
    catalog.part("GASKET", article="G-4")
    path = write_csv(tmp_path, [("GASKET OLD NAME", "Прокладка", "APPROVE", "BRP|нет", "")])

    with pytest.raises(CommandError, match="не попала ни одна деталь"):
        call_command("apply_confirmed_russian_names", path, "--apply")


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(CommandError, match="Не удалось открыть файл"):
        call_command("apply_confirmed_russian_names", str(tmp_path / "нет.csv"))


# --- Область действия решения -----------------------------------------------------------


def test_the_decision_stays_inside_its_own_group(catalog, tmp_path):
    """Один текст названия у двух производителей это две разные группы."""
    brp = catalog.part("BALL BEARING", article="BB-1", maker="BRP")
    other = catalog.part("BALL BEARING", article="BB-2", maker="WISECO")
    path = write_csv(
        tmp_path, [("BALL BEARING", "Подшипник шариковый", "APPROVE", "BRP|нет", "")]
    )

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(brp) == ("Подшипник шариковый", True)
    assert russian_of(other) == ("", False), "чужая группа не тронута"


def test_one_decision_covers_every_part_of_its_group(catalog, tmp_path):
    parts = [catalog.part("OIL SEAL", article=f"OS-{index}") for index in range(4)]
    path = write_csv(tmp_path, [("OIL SEAL", "Сальник", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert all(russian_of(part) == ("Сальник", True) for part in parts)


def test_the_catalog_half_of_the_selector_is_honoured(catalog, tmp_path):
    linked = catalog.part("SPRING", article="SP-1")
    catalog.brp(linked, "SP-1")
    loose = catalog.part("SPRING", article="SP-2")
    path = write_csv(tmp_path, [("SPRING", "Пружина", "APPROVE", "BRP|BRP", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(linked) == ("Пружина", True)
    assert russian_of(loose) == ("", False)


# --- Защита уже подтверждённого ----------------------------------------------------------


def test_an_already_confirmed_name_is_protected(catalog, tmp_path):
    part = catalog.part("GASKET", article="G-5")
    PartCustomsInfo.objects.create(
        part_type=part, customs_name_ru="Прокладка головки", customs_name_ru_confirmed=True
    )
    path = write_csv(tmp_path, [("GASKET", "Прокладка", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(part) == ("Прокладка головки", True)


def test_allow_replace_lets_the_owner_change_it(catalog, tmp_path):
    part = catalog.part("GASKET", article="G-6")
    PartCustomsInfo.objects.create(
        part_type=part, customs_name_ru="Прокладка головки", customs_name_ru_confirmed=True
    )
    path = write_csv(tmp_path, [("GASKET", "Прокладка", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply", "--allow-replace")

    assert russian_of(part) == ("Прокладка", True)


def test_an_unconfirmed_draft_is_replaced_without_a_flag(catalog, tmp_path):
    part = catalog.part("GASKET", article="G-7")
    PartCustomsInfo.objects.create(
        part_type=part, customs_name_ru="черновик автоперевода", customs_name_ru_confirmed=False
    )
    path = write_csv(tmp_path, [("GASKET", "Прокладка", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(part) == ("Прокладка", True)


# --- Идемпотентность и отсутствие побочных эффектов ---------------------------------------


def test_running_twice_changes_nothing_the_second_time(catalog, tmp_path):
    part = catalog.part("OIL SEAL", article="OS-9")
    path = write_csv(tmp_path, [("OIL SEAL", "Сальник", "APPROVE", "BRP|нет", "")])
    call_command("apply_confirmed_russian_names", path, "--apply")
    versions = PartCustomsDataVersion.objects.filter(part_type=part).count()
    updated_at = PartCustomsInfo.objects.get(part_type=part).updated_at

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert russian_of(part) == ("Сальник", True)
    assert PartCustomsDataVersion.objects.filter(part_type=part).count() == versions
    assert PartCustomsInfo.objects.get(part_type=part).updated_at == updated_at


def test_nothing_but_the_russian_name_is_touched(catalog, tmp_path):
    part = catalog.part("BUSHING", article="BU-9", price="1234.00")
    info = PartCustomsInfo.objects.create(
        part_type=part,
        gross_weight_kg=Decimal("1.5"),
        net_weight_kg=Decimal("1.2"),
        application_area=PartCustomsInfo.ApplicationArea.SNOWMOBILE,
        customs_name_en="BUSHING",
    )
    path = write_csv(tmp_path, [("BUSHING", "Втулка", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    part.refresh_from_db()
    info.refresh_from_db()
    assert part.recommended_price == Decimal("1234.00")
    assert (info.gross_weight_kg, info.net_weight_kg) == (Decimal("1.5"), Decimal("1.2"))
    assert info.application_area == PartCustomsInfo.ApplicationArea.SNOWMOBILE
    assert info.customs_name_en == "BUSHING"


def test_the_decision_is_recorded_in_the_customs_history(catalog, tmp_path):
    part = catalog.part("OIL SEAL", article="OS-10")
    path = write_csv(tmp_path, [("OIL SEAL", "Сальник", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    version = PartCustomsDataVersion.objects.filter(part_type=part).latest("version")
    assert version.customs_name_ru == "Сальник"
    assert version.customs_name_ru_confirmed is True


# --- Название сразу ищется ---------------------------------------------------------------


def test_the_confirmed_name_is_searchable_at_once(catalog, tmp_path):
    part = catalog.part("OIL SEAL", article="OS-11")
    assert search_part_ids("сальник") == []
    path = write_csv(tmp_path, [("OIL SEAL", "Сальник", "APPROVE", "BRP|нет", "")])

    call_command("apply_confirmed_russian_names", path, "--apply")

    assert [hit.part_id for hit in search_part_ids("сальник")] == [part.pk]
    assert [hit.part_id for hit in search_part_ids("САЛЬНИК")] == [part.pk]
    assert build_public_part_facts([part.pk])[0].russian_name == "Сальник"
    operator = resolve_part_lookup(
        "сальник", allow_partial=True, allow_name=True, allow_confirmed_ru_name=True
    )
    assert part.pk in {candidate.part.pk for candidate in operator.candidates}
