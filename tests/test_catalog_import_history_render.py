"""История импортов каталога обязана открываться при любой форме сводки.

В истории лежат партии разных видов, и сводки у них разные. Обычная проверка
файла, применённый полный снимок, коррекция цен, неудачная проверка и старые
записи с неполной сводкой хранят разный набор ключей.

Отсутствующий необязательный ключ не является ошибкой: у коррекции цен нет
понятия «перестанут быть актуальными», потому что коррекция актуальность строк
не меняет вовсе. Такую метрику нужно не показывать, а не показывать нулём:
ноль здесь означал бы «ничего не деактивировано», хотя правильный ответ
«неприменимо».
"""
from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog_import.models import CatalogImportBatch

PASSWORD = "parol-12345"

# Сводка обычной проверки файла: полный набор счётчиков импортёра.
CHECK_SUMMARY = {
    "data_rows": 130000,
    "unique_materials": 129500,
    "created": 120,
    "updated": 2400,
    "reactivated": 5,
    "deactivated": 310,
    "skipped_unchanged": 126000,
    "skipped_empty": 0,
    "duplicates": 12,
    "status_counts": {"USE": 1000, "OBS": 20},
}

# Сводка применённого полного снимка.
APPLY_SUMMARY = {
    **CHECK_SUMMARY,
    "new_file_nonzero_price": 111000,
    "previous_catalog_price_retained": 520,
    "no_usable_price": 17743,
}

# Сводка коррекции: свой набор ключей, deactivated отсутствует по смыслу.
CORRECTION_SUMMARY = {
    "kind": "brp_zero_wholesale_correction",
    "applied_at": "2026-08-19T10:00:00+00:00",
    "source_batch_id": 2,
    "source_filename": "BRP-2026-08.xlsx",
    "source_sha256": "a" * 64,
    "previous_filename": "BRP-2026-07.xlsx",
    "previous_sha256": "b" * 64,
    "current_materials": 129500,
    "same_file_nonzero": 0,
    "previous_catalog_fallback": 520,
    "no_usable_price": 17743,
    "ambiguous_nonzero": 0,
    "invalid_or_negative": 0,
    "rows_to_update": 18263,
    "linked_prices_refreshed": 41,
    "status_counts": {"USE": 900},
    "samples": [{"material_no": "1234567", "previous": "10.50", "selected": "10.50"}],
}


@pytest.fixture
def manager(db, django_user_model):
    user = django_user_model.objects.create_user(username="upravlyayushiy", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.MANAGER))
    return user


@pytest.fixture
def logged_in(client, manager):
    client.login(username=manager.username, password=PASSWORD)
    return client


def _batch(**kwargs) -> CatalogImportBatch:
    defaults = {
        "catalog": CatalogImportBatch.Catalog.BRP,
        "source_filename": "BRP.xlsx",
        "source_sha256": "c" * 64,
        "source_size": 1024,
    }
    return CatalogImportBatch.objects.create(**{**defaults, **kwargs})


def _counter_cells(response) -> list[str]:
    """Содержимое числовых ячеек истории, без разметки и отступов."""
    body = response.content.decode()
    return [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r'<td class="num">(.*?)</td>', body, re.S)
    ]


def _list(client):
    return client.get(reverse("catalog_import_list"))


def _detail(client, batch):
    return client.get(reverse("catalog_import_detail", args=[batch.pk]))


# --- A: обычный применённый полный снимок --------------------------------------------------


def test_applied_snapshot_shows_its_counters(logged_in):
    batch = _batch(
        status=CatalogImportBatch.Status.APPLIED,
        summary=CHECK_SUMMARY,
        apply_summary=APPLY_SUMMARY,
    )
    resp = _list(logged_in)
    assert resp.status_code == 200

    body = resp.content.decode()
    assert "130 000" in body or "130000" in body, "не показано число строк данных"
    assert "2 400" in body or "2400" in body, "не показано число изменённых"
    assert "310" in body, "не показано число ставших неактуальными"
    assert _detail(logged_in, batch).status_code == 200


def test_a_real_zero_counter_is_shown_as_zero(logged_in):
    """Ноль это ответ «ничего не изменилось», и его нужно показывать числом.

    Прочерк на этом месте означал бы «метрика неприменима», а это другое
    утверждение.
    """
    _batch(
        status=CatalogImportBatch.Status.APPLIED,
        summary={**CHECK_SUMMARY, "created": 0, "deactivated": 0},
        apply_summary={**APPLY_SUMMARY, "created": 0, "deactivated": 0},
    )
    resp = _list(logged_in)
    assert resp.status_code == 200
    cells = _counter_cells(resp)
    assert cells == ["130000", "0", "2400", "0"], (
        f"настоящий ноль показан не числом: {cells}"
    )


# --- B, C: коррекция ------------------------------------------------------------------------


def test_correction_batch_renders_without_a_deactivated_key(logged_in):
    """Главный случай отказа: у коррекции ключа deactivated нет вовсе."""
    batch = _batch(
        status=CatalogImportBatch.Status.APPLIED,
        source_filename="Коррекция нулевых wholesale: batch #2",
        summary=CORRECTION_SUMMARY,
        apply_summary=CORRECTION_SUMMARY,
    )
    resp = _list(logged_in)
    assert resp.status_code == 200
    assert _detail(logged_in, batch).status_code == 200


def test_correction_does_not_claim_zero_deactivated(logged_in):
    """Коррекция не меняет актуальность строк, поэтому ноль здесь был бы ложью."""
    _batch(
        status=CatalogImportBatch.Status.APPLIED,
        source_filename="Коррекция нулевых wholesale: batch #2",
        summary=CORRECTION_SUMMARY,
        apply_summary=CORRECTION_SUMMARY,
    )
    resp = _list(logged_in)
    assert "Коррекция нулевых wholesale" in resp.content.decode()
    cells = _counter_cells(resp)
    assert cells == ["·", "·", "·", "·"], (
        f"у коррекции показаны числа там, где метрики неприменимы: {cells}"
    )


def test_correction_shows_its_own_meaningful_counters(logged_in):
    """Свои счётчики коррекции должны быть видны, а не потеряны."""
    batch = _batch(
        status=CatalogImportBatch.Status.APPLIED,
        source_filename="Коррекция нулевых wholesale: batch #2",
        summary=CORRECTION_SUMMARY,
        apply_summary=CORRECTION_SUMMARY,
    )
    body = _detail(logged_in, batch).content.decode()
    assert "18 263" in body or "18263" in body, "не показано число исправленных строк"
    assert "520" in body, "не показан перенос цены предыдущего каталога"


# --- D: старые записи с неполной сводкой ---------------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        {},
        {"data_rows": 100},
        {"created": 1, "updated": 2},
        {"data_rows": 5, "status_counts": {}},
    ],
)
def test_a_historical_batch_with_a_partial_summary_still_opens(logged_in, summary):
    batch = _batch(status=CatalogImportBatch.Status.CHECKED, summary=summary)
    assert _list(logged_in).status_code == 200
    assert _detail(logged_in, batch).status_code == 200


# --- E: неудачные партии --------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        CatalogImportBatch.Status.UPLOADED,
        CatalogImportBatch.Status.CHECK_FAILED,
        CatalogImportBatch.Status.APPLY_FAILED,
    ],
)
def test_a_failed_batch_opens_with_its_error(logged_in, status):
    batch = _batch(status=status, summary={}, error_text="Файл повреждён на строке 5.")
    assert _list(logged_in).status_code == 200

    resp = _detail(logged_in, batch)
    assert resp.status_code == 200
    assert "Файл повреждён" in resp.content.decode()


# --- Всё вместе: одна страница со всеми видами партий ---------------------------------------


def test_every_batch_kind_renders_on_one_page(logged_in):
    """Реальная история смешанная, и открываться она должна целиком."""
    _batch(status=CatalogImportBatch.Status.CHECKED, summary=CHECK_SUMMARY)
    _batch(
        status=CatalogImportBatch.Status.APPLIED,
        summary=CHECK_SUMMARY,
        apply_summary=APPLY_SUMMARY,
    )
    _batch(
        status=CatalogImportBatch.Status.APPLIED,
        source_filename="Коррекция нулевых wholesale: batch #2",
        summary=CORRECTION_SUMMARY,
        apply_summary=CORRECTION_SUMMARY,
    )
    _batch(status=CatalogImportBatch.Status.CHECK_FAILED, summary={}, error_text="Сбой")
    _batch(status=CatalogImportBatch.Status.UPLOADED)

    resp = _list(logged_in)
    assert resp.status_code == 200
    assert len(resp.context["page_obj"].object_list) == 5

    for batch in CatalogImportBatch.objects.all():
        assert _detail(logged_in, batch).status_code == 200, f"партия #{batch.pk} не открылась"


def test_the_counter_sources_are_left_as_they_were(logged_in):
    """Исправление отображения не должно менять сами числа.

    Три первых числа истории показывают разбор файла, а «Неактуально» результат
    применения. Выбор источника оставлен прежним намеренно: сотрудник уже видел
    эти числа, и молча подменять их на другие при починке рендера нельзя.
    """
    _batch(
        status=CatalogImportBatch.Status.APPLIED,
        summary={**CHECK_SUMMARY, "created": 7, "updated": 8, "deactivated": 9},
        apply_summary={**APPLY_SUMMARY, "created": 70, "updated": 80, "deactivated": 90},
    )
    cells = _counter_cells(_list(logged_in))
    assert cells == ["130000", "7", "8", "90"], (
        f"источник чисел истории изменился: {cells}"
    )
