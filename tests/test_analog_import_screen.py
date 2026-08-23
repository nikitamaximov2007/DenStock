"""Импорт аналогов через экран: загрузка, предпросмотр, применение.

Рабочий процесс общий с прайсом поставщика, поэтому здесь проверяется не он
целиком, а то, что каталог аналогов в него встроен правильно: проверка ничего
не пишет, спорные строки видно с номерами, применение делает ровно обещанное, а
повторная загрузка того же файла не удваивает.
"""
from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from apps.accounts import roles
from apps.catalog.models import PartAnalog, PartType
from apps.catalog.services import create_manual_part
from apps.catalog_import.models import CatalogImportBatch
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement

PASSWORD = "parol-12345"
HEADERS = [
    "Исходный артикул", "Артикул аналога", "Название аналога",
    "Цена", "Производитель аналога", "Штрихкод аналога",
]


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def boss(client, make_user):
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)
    return client


@pytest.fixture
def original(db):
    return create_manual_part(
        name="Поршень BRP", article="SAME-001", price=Decimal("10000"),
        manufacturer_name="BRP",
    )


def upload_file(rows, *, name="analogs.xlsx"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(list(row))
    buffer = BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile(
        name, buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _stock_snapshot():
    return (
        StockMovement.objects.count(),
        StockLot.objects.count(),
        PartItem.objects.count(),
        StockBalance.objects.count(),
    )


def send(client, rows, **kwargs):
    return client.post(
        reverse("catalog_import_upload"),
        {"catalog": "analogs", "workbook": upload_file(rows, **kwargs)},
        follow=True,
    )


# --- Загрузка и предпросмотр --------------------------------------------------------


def test_the_analog_catalog_is_offered_on_the_upload_screen(boss, original):
    body = boss.get(reverse("catalog_import_list")).content.decode()
    assert 'value="analogs"' in body
    assert "Аналоги" in body


def test_checking_a_file_writes_nothing(boss, original):
    before = PartType.objects.count(), PartAnalog.objects.count(), _stock_snapshot()

    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])

    batch = CatalogImportBatch.objects.get()
    assert batch.status == CatalogImportBatch.Status.CHECKED
    assert (PartType.objects.count(), PartAnalog.objects.count(), _stock_snapshot()) == before


def test_the_preview_says_what_will_happen(boss, original):
    send(boss, [
        ["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""],
        ["SAME-001", "ALT-003", "Поршень ALT", "5000", "ALT", ""],
    ])
    batch = CatalogImportBatch.objects.get()

    body = boss.get(reverse("catalog_import_detail", args=[batch.pk])).content.decode()

    assert "Строк в файле" in body
    assert "Новых деталей" in body
    assert "Новых связей" in body


def test_a_problem_row_is_shown_with_its_number(boss, original):
    send(boss, [
        ["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""],
        ["НЕТ-ТАКОГО", "ALT-003", "Поршень ALT", "5000", "ALT", ""],
    ])
    batch = CatalogImportBatch.objects.get()

    body = boss.get(reverse("catalog_import_detail", args=[batch.pk])).content.decode()

    assert "Строки, требующие внимания" in body
    assert "Не найдена исходная деталь" in body
    assert ">3<" in body, "не видно, какая именно строка файла"


def test_a_file_with_nothing_to_apply_is_refused_with_words(boss, original):
    response = send(boss, [["НЕТ-ТАКОГО", "A-1", "Аналог", "100", "", ""]])

    assert response.status_code == 200
    batch = CatalogImportBatch.objects.get()
    assert batch.status == CatalogImportBatch.Status.CHECK_FAILED
    assert "нет ни одной строки" in batch.error_text.lower()


def test_a_file_with_wrong_columns_does_not_crash(boss, original):
    workbook = Workbook()
    workbook.active.append(["Совсем", "не", "то"])
    buffer = BytesIO()
    workbook.save(buffer)
    upload = SimpleUploadedFile("bad.xlsx", buffer.getvalue())

    response = boss.post(
        reverse("catalog_import_upload"),
        {"catalog": "analogs", "workbook": upload},
        follow=True,
    )

    assert response.status_code == 200
    assert "не хватает" in response.content.decode().lower()


def test_a_file_that_is_not_excel_does_not_crash(boss, original):
    upload = SimpleUploadedFile("fake.xlsx", b"\x00\x01 not a workbook")
    response = boss.post(
        reverse("catalog_import_upload"),
        {"catalog": "analogs", "workbook": upload},
        follow=True,
    )
    assert response.status_code == 200


# --- Применение ---------------------------------------------------------------------


def test_applying_creates_exactly_what_the_preview_promised(boss, original):
    send(boss, [
        ["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""],
        ["SAME-001", "ALT-003", "Поршень ALT", "5000", "ALT", ""],
    ])
    batch = CatalogImportBatch.objects.get()
    assert batch.counter("will_create_parts") == 2

    response = boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)

    assert response.status_code == 200
    batch.refresh_from_db()
    assert batch.status == CatalogImportBatch.Status.APPLIED
    assert PartType.objects.count() == 3
    assert PartAnalog.objects.count() == 2


def test_applying_never_creates_stock(boss, original):
    before = _stock_snapshot()
    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])
    batch = CatalogImportBatch.objects.get()

    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)

    assert _stock_snapshot() == before


def test_applying_twice_is_refused(boss, original):
    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])
    batch = CatalogImportBatch.objects.get()
    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)

    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)

    assert PartAnalog.objects.count() == 1
    assert PartType.objects.count() == 2


def test_the_same_file_uploaded_again_creates_nothing_new(boss, original):
    rows = [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]]
    send(boss, rows)
    first = CatalogImportBatch.objects.get()
    boss.post(reverse("catalog_import_apply", args=[first.pk]), follow=True)

    send(boss, rows)
    second = CatalogImportBatch.objects.exclude(pk=first.pk).get()
    boss.post(reverse("catalog_import_apply", args=[second.pk]), follow=True)

    assert PartType.objects.count() == 2
    assert PartAnalog.objects.count() == 1


def test_the_screen_warns_that_the_same_file_was_already_applied(boss, original):
    rows = [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]]
    send(boss, rows)
    first = CatalogImportBatch.objects.get()
    boss.post(reverse("catalog_import_apply", args=[first.pk]), follow=True)

    send(boss, rows)
    second = CatalogImportBatch.objects.exclude(pk=first.pk).get()
    body = boss.get(reverse("catalog_import_detail", args=[second.pk])).content.decode()

    assert "применял" in body.lower() or "уже" in body.lower()


def test_the_screen_does_not_promise_a_deletion_that_will_not_happen(boss, original):
    """Найдено при осмотре: экран показывал предупреждение от прайса поставщика.

    Там файл считается полным срезом, и позиции вне его перестают быть
    текущими. Каталог аналогов так не работает вовсе, и обещать это значило бы
    пугать человека несуществующим удалением.
    """
    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])
    batch = CatalogImportBatch.objects.get()

    body = boss.get(reverse("catalog_import_detail", args=[batch.pk])).content.decode()

    assert "перестанут использоваться" not in body
    assert "полным актуальным каталогом" not in body
    assert "Ничего не удаляется" in body


def test_the_supplier_price_keeps_its_own_warning(boss, original):
    """Проверка проверки: у прайса предупреждение обязано остаться."""
    batch = CatalogImportBatch.objects.create(
        catalog=CatalogImportBatch.Catalog.BRP,
        status=CatalogImportBatch.Status.CHECKED,
        source_filename="brp.xlsx", source_sha256="0" * 64,
    )
    body = boss.get(reverse("catalog_import_detail", args=[batch.pk])).content.decode()
    assert "полным актуальным каталогом" in body


def test_the_applied_summary_says_what_happened(boss, original):
    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])
    batch = CatalogImportBatch.objects.get()
    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)

    body = boss.get(reverse("catalog_import_detail", args=[batch.pk])).content.decode()

    assert "Заведено деталей" in body
    assert "Создано связей" in body


# --- Права ---------------------------------------------------------------------------


def test_a_storekeeper_cannot_reach_the_import(client, make_user, original):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)

    assert client.get(reverse("catalog_import_list")).status_code == 403
    response = client.post(
        reverse("catalog_import_upload"),
        {"catalog": "analogs", "workbook": upload_file([["SAME-001", "A", "Б", "1", "", ""]])},
    )
    assert response.status_code == 403
    assert not CatalogImportBatch.objects.exists()


def test_a_storekeeper_cannot_apply_someone_elses_batch(client, boss, make_user, original):
    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])
    batch = CatalogImportBatch.objects.get()

    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    assert client.post(
        reverse("catalog_import_apply", args=[batch.pk])
    ).status_code == 403
    assert not PartAnalog.objects.exists()


def test_the_import_cannot_be_applied_by_a_plain_link(boss, original):
    """Мутация только методом POST: обновление страницы ничего не применит."""
    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])
    batch = CatalogImportBatch.objects.get()

    assert boss.get(reverse("catalog_import_apply", args=[batch.pk])).status_code == 405
    assert not PartAnalog.objects.exists()


# --- BRP не задет ---------------------------------------------------------------------


def test_the_analog_import_leaves_the_supplier_catalog_alone(boss, original):
    from apps.brp.models import BrpCatalogPart

    before = BrpCatalogPart.objects.count()
    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])
    batch = CatalogImportBatch.objects.get()
    boss.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)

    assert BrpCatalogPart.objects.count() == before


def test_the_two_catalogs_keep_their_own_batches(boss, original):
    send(boss, [["SAME-001", "ALT-002", "Поршень XYZ", "4500", "XYZ", ""]])
    batch = CatalogImportBatch.objects.get()
    assert batch.catalog == CatalogImportBatch.Catalog.ANALOGS
    assert batch.get_catalog_display() == "Аналоги"
