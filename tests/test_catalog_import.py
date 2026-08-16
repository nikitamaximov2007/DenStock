"""Штатный импорт каталога из Excel: загрузка, проверка, применение.

Главные гарантии:

* проверка ничего не меняет в справочнике;
* применить можно только проверенную партию, и только если файл и каталог с
  момента проверки не изменились;
* повторная загрузка того же файла безопасна, повторное применение отклоняется;
* импорт каталога НЕ трогает склад: движения, остатки, лоты и ячейки остаются
  прежними;
* исторические цены проведённых документов не переписываются;
* неизвестный статус поставщика виден, а не проглатывается.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from apps.brp.models import BrpCatalogPart
from apps.catalog_import.adapters import inspect_workbook
from apps.catalog_import.models import CatalogImportBatch
from apps.catalog_import.services import (
    CatalogImportError,
    apply_batch,
    run_check,
    save_upload,
)
from apps.inventory.models import StockMovement

PASSWORD = "parol-12345"

HEADERS = [
    "Material_No", "Part_Desc", "Last_Yr_Util", "Status",
    "РОЗНИЦА", "ОПТОВАЯ", "ЗАМЕНА НОМЕРА", "ЗАМЕНА НОМЕРА",
]
NOTE_ROW = ["", "", "", "", "", "", "", ""]


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
def admin(make_user):
    return make_user("admin", is_superuser=True)


def _workbook(rows, tmp_path, *, name="brp.xlsx", headers=HEADERS, note=True):
    """Файл в реальном формате BRP: заголовки, строка примечаний, данные."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    if note:
        sheet.append(NOTE_ROW)
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    return path


def _upload(path, name=None):
    return SimpleUploadedFile(
        name or path.name,
        path.read_bytes(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _batch(path, admin, settings, tmp_path, *, name=None):
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    return save_upload(_upload(path, name), catalog="brp", by=admin)


def _login(client, make_user, *, role=None, superuser=True, name="boss"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


ROW_OK = ["420831955", "ROLLER", "2025", "", "25.99", "20.00", "", ""]
ROW_OBS = ["420931284", "DAMPER", "2024", "OBS", "31.00", "24.00", "", ""]
ROW_USE = ["420931285", "BELT", "2024", "USE", "40.00", "31.00", "420931999", ""]
ROW_VIN = ["420931795", "PULLEY", "2019", "VIN", "80.00", "62.00", "", ""]
ROW_LIQ = ["420931796", "SPRING", "2023", "LIQ", "12.00", "9.00", "", ""]


# --- Разбор файла -----------------------------------------------------------------------


def test_valid_workbook_is_parsed(db, admin, settings, tmp_path):
    path = _workbook([ROW_OK, ROW_OBS], tmp_path)
    batch = run_check(_batch(path, admin, settings, tmp_path))
    assert batch.status == CatalogImportBatch.Status.CHECKED
    assert batch.summary["data_rows"] == 2
    assert batch.summary["created"] == 2


def test_empty_workbook_is_reported(db, admin, settings, tmp_path):
    path = _workbook([], tmp_path)
    batch = run_check(_batch(path, admin, settings, tmp_path))
    assert batch.summary["data_rows"] == 0
    assert batch.summary["created"] == 0


def test_wrong_extension_is_rejected(db, admin, settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    upload = SimpleUploadedFile("price.csv", b"a;b;c", content_type="text/csv")
    with pytest.raises(CatalogImportError):
        save_upload(upload, catalog="brp", by=admin)


def test_empty_file_is_rejected(db, admin, settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    upload = SimpleUploadedFile("price.xlsx", b"", content_type="application/vnd.ms-excel")
    with pytest.raises(CatalogImportError):
        save_upload(upload, catalog="brp", by=admin)


def test_unknown_catalog_is_rejected(db, admin, settings, tmp_path):
    from apps.catalog_import.adapters import CatalogAdapterError

    path = _workbook([ROW_OK], tmp_path)
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    with pytest.raises(CatalogAdapterError):
        save_upload(_upload(path), catalog="unknown-brand", by=admin)


def test_leading_zero_part_number_is_kept_as_text(db, admin, settings, tmp_path):
    path = _workbook([["007123456", "SMALL", "", "", "1.00", "0.80", "", ""]], tmp_path)
    apply_batch(run_check(_batch(path, admin, settings, tmp_path)), by=admin)
    assert BrpCatalogPart.objects.filter(material_no="007123456").exists()


def test_whitespace_is_trimmed(db, admin, settings, tmp_path):
    path = _workbook([["  420831955  ", "  ROLLER  ", "", "", "1.00", "0.80", "", ""]], tmp_path)
    apply_batch(run_check(_batch(path, admin, settings, tmp_path)), by=admin)
    part = BrpCatalogPart.objects.get()
    assert part.material_no == "420831955"
    assert part.part_desc == "ROLLER"


def test_numeric_coercion_keeps_number_as_text(db, admin, settings, tmp_path):
    # Excel часто отдаёт номер числом: он обязан остаться строкой без ".0".
    path = _workbook([[420831955, "ROLLER", "", "", 25.99, 20.0, "", ""]], tmp_path)
    apply_batch(run_check(_batch(path, admin, settings, tmp_path)), by=admin)
    part = BrpCatalogPart.objects.get()
    assert part.material_no == "420831955"
    assert part.wholesale_price_usd == Decimal("20.00")


def test_empty_and_invalid_price_do_not_break_import(db, admin, settings, tmp_path):
    rows = [
        ["420831955", "NO PRICE", "", "", "", "", "", ""],
        ["420831956", "BAD PRICE", "", "", "нет", "нет", "", ""],
    ]
    batch = run_check(_batch(_workbook(rows, tmp_path), admin, settings, tmp_path))
    apply_batch(batch, by=admin)
    assert BrpCatalogPart.objects.count() == 2
    assert BrpCatalogPart.objects.get(material_no="420831956").wholesale_price_usd is None


def test_duplicate_part_number_keeps_priced_row(db, admin, settings, tmp_path):
    rows = [
        ["420831955", "ROLLER", "", "", "25.99", "0", "", ""],
        ["420831955", "ROLLER", "", "", "25.99", "20.00", "", ""],
    ]
    batch = run_check(_batch(_workbook(rows, tmp_path), admin, settings, tmp_path))
    apply_batch(batch, by=admin)
    assert BrpCatalogPart.objects.count() == 1
    assert BrpCatalogPart.objects.get().wholesale_price_usd == Decimal("20.00")


# --- Статусы поставщика -------------------------------------------------------------------


def test_supplier_statuses_are_stored(db, admin, settings, tmp_path):
    rows = [ROW_OBS, ROW_USE, ROW_VIN, ROW_LIQ, ROW_OK]
    batch = run_check(_batch(_workbook(rows, tmp_path), admin, settings, tmp_path))
    apply_batch(batch, by=admin)
    statuses = dict(BrpCatalogPart.objects.values_list("material_no", "brp_status"))
    assert statuses["420931284"] == "OBS"
    assert statuses["420931285"] == "USE"
    assert statuses["420931795"] == "VIN"
    assert statuses["420931796"] == "LIQ"
    assert statuses["420831955"] == ""  # пустой статус остаётся пустым


def test_status_counts_are_reported_in_summary(db, admin, settings, tmp_path):
    rows = [ROW_OBS, ROW_VIN, ROW_OK]
    batch = run_check(_batch(_workbook(rows, tmp_path), admin, settings, tmp_path))
    counts = batch.summary["status_counts"]
    assert counts.get("OBS") == 1
    assert counts.get("VIN") == 1


def test_unknown_status_is_visible_and_not_treated_as_obsolete(db, admin, settings, tmp_path):
    rows = [["420831955", "ROLLER", "", "ZZZ", "1.00", "0.80", "", ""]]
    batch = run_check(_batch(_workbook(rows, tmp_path), admin, settings, tmp_path))
    assert batch.summary["status_counts"].get("ZZZ") == 1
    apply_batch(batch, by=admin)
    part = BrpCatalogPart.objects.get()
    assert part.brp_status == "ZZZ"  # сохранён как есть
    assert part.status_label == "ZZZ"  # без выдуманной расшифровки


def test_use_status_keeps_replacement_number(db, admin, settings, tmp_path):
    batch = run_check(_batch(_workbook([ROW_USE], tmp_path), admin, settings, tmp_path))
    apply_batch(batch, by=admin)
    part = BrpCatalogPart.objects.get()
    assert part.replacement_no_1 == "420931999"
    assert part.brp_status == "USE"


def test_obsolete_part_is_never_deleted_on_reimport(db, admin, settings, tmp_path):
    first = _workbook([ROW_OBS], tmp_path, name="first.xlsx")
    apply_batch(run_check(_batch(first, admin, settings, tmp_path)), by=admin)
    # Следующий прайс этой позиции больше не содержит.
    second = _workbook([ROW_OK], tmp_path, name="second.xlsx")
    apply_batch(run_check(_batch(second, admin, settings, tmp_path)), by=admin)
    assert BrpCatalogPart.objects.filter(material_no="420931284").exists()


# --- Проверка ничего не меняет -------------------------------------------------------------


def test_check_does_not_write_anything(db, admin, settings, tmp_path):
    batch = _batch(_workbook([ROW_OK, ROW_OBS], tmp_path), admin, settings, tmp_path)
    run_check(batch)
    assert BrpCatalogPart.objects.count() == 0


def test_check_reports_price_change_before_apply(db, admin, settings, tmp_path):
    first = _workbook([ROW_OK], tmp_path, name="first.xlsx")
    apply_batch(run_check(_batch(first, admin, settings, tmp_path)), by=admin)
    changed = ["420831955", "ROLLER", "2025", "", "25.99", "22.00", "", ""]
    second = _workbook([changed], tmp_path, name="second.xlsx")
    batch = run_check(_batch(second, admin, settings, tmp_path))
    assert batch.summary["updated"] == 1
    # До применения цена в справочнике прежняя.
    assert BrpCatalogPart.objects.get().wholesale_price_usd == Decimal("20.00")
    apply_batch(batch, by=admin)
    assert BrpCatalogPart.objects.get().wholesale_price_usd == Decimal("22.00")


# --- Безопасность применения ---------------------------------------------------------------


def test_apply_requires_successful_check(db, admin, settings, tmp_path):
    batch = _batch(_workbook([ROW_OK], tmp_path), admin, settings, tmp_path)
    with pytest.raises(CatalogImportError):
        apply_batch(batch, by=admin)


def test_apply_twice_is_rejected(db, admin, settings, tmp_path):
    batch = run_check(_batch(_workbook([ROW_OK], tmp_path), admin, settings, tmp_path))
    apply_batch(batch, by=admin)
    with pytest.raises(CatalogImportError):
        apply_batch(batch, by=admin)
    assert BrpCatalogPart.objects.count() == 1


def test_stale_catalog_blocks_apply(db, admin, settings, tmp_path):
    batch = run_check(_batch(_workbook([ROW_OK], tmp_path), admin, settings, tmp_path))
    # Каталог изменили между проверкой и применением.
    BrpCatalogPart.objects.create(material_no="999999999", part_desc="OTHER")
    with pytest.raises(CatalogImportError) as exc:
        apply_batch(batch, by=admin)
    assert "STALE_DRY_RUN" in str(exc.value)


def test_stale_file_blocks_apply(db, admin, settings, tmp_path):
    from apps.catalog_import.services import stored_file_path

    batch = run_check(_batch(_workbook([ROW_OK], tmp_path), admin, settings, tmp_path))
    stored_file_path(batch).write_bytes(b"other-bytes")
    with pytest.raises(CatalogImportError) as exc:
        apply_batch(batch, by=admin)
    assert "STALE_DRY_RUN" in str(exc.value)


def test_recheck_after_catalog_change_allows_apply(db, admin, settings, tmp_path):
    batch = run_check(_batch(_workbook([ROW_OK], tmp_path), admin, settings, tmp_path))
    BrpCatalogPart.objects.create(material_no="999999999", part_desc="OTHER")
    run_check(batch)
    apply_batch(batch, by=admin)
    assert batch.status == CatalogImportBatch.Status.APPLIED


def test_same_file_twice_is_idempotent(db, admin, settings, tmp_path):
    path = _workbook([ROW_OK, ROW_OBS], tmp_path)
    apply_batch(run_check(_batch(path, admin, settings, tmp_path)), by=admin)
    count_after_first = BrpCatalogPart.objects.count()
    second = run_check(_batch(path, admin, settings, tmp_path))
    assert second.summary["created"] == 0
    assert second.summary["skipped_unchanged"] == 2
    apply_batch(second, by=admin)
    assert BrpCatalogPart.objects.count() == count_after_first


def test_batch_records_file_identity(db, admin, settings, tmp_path):
    path = _workbook([ROW_OK], tmp_path)
    batch = _batch(path, admin, settings, tmp_path)
    assert len(batch.source_sha256) == 64
    assert batch.source_size > 0
    assert batch.created_by == admin


# --- Инвариант склада ----------------------------------------------------------------------


def test_import_never_touches_warehouse(db, admin, settings, tmp_path):
    from apps.inventory.models import StockBalance, StockLot

    movements_before = StockMovement.objects.count()
    lots_before = StockLot.objects.count()
    balances_before = StockBalance.objects.count()

    rows = [ROW_OK, ROW_OBS, ROW_USE, ROW_VIN, ROW_LIQ]
    apply_batch(run_check(_batch(_workbook(rows, tmp_path), admin, settings, tmp_path)), by=admin)

    assert StockMovement.objects.count() == movements_before
    assert StockLot.objects.count() == lots_before
    assert StockBalance.objects.count() == balances_before


# --- Инспектор книги -----------------------------------------------------------------------


def test_inspector_reports_structure(tmp_path):
    path = _workbook([ROW_OK, ROW_OBS], tmp_path)
    structure = inspect_workbook(path)
    assert structure["headers"][0] == "Material_No"
    assert structure["sheet"] in structure["sheets"]
    assert len(structure["sample_rows"]) >= 1


def test_inspector_rejects_non_xlsx(tmp_path):
    from apps.catalog_import.adapters import CatalogAdapterError

    path = tmp_path / "price.csv"
    path.write_text("a;b", encoding="utf-8")
    with pytest.raises(CatalogAdapterError):
        inspect_workbook(path)


# --- Экраны и права ------------------------------------------------------------------------


def test_workflow_through_ui(client, make_user, db, settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    _login(client, make_user)
    path = _workbook([ROW_OK, ROW_OBS], tmp_path)

    resp = client.post(
        reverse("catalog_import_upload"),
        {"catalog": "brp", "workbook": _upload(path)},
        follow=True,
    )
    assert resp.status_code == 200
    batch = CatalogImportBatch.objects.get()
    assert batch.status == CatalogImportBatch.Status.CHECKED
    assert BrpCatalogPart.objects.count() == 0  # проверка ничего не применила

    page = client.get(reverse("catalog_import_detail", args=[batch.pk])).content.decode()
    assert "Что изменится" in page
    assert "Применить импорт" in page

    client.post(reverse("catalog_import_apply", args=[batch.pk]), follow=True)
    batch.refresh_from_db()
    assert batch.status == CatalogImportBatch.Status.APPLIED
    assert BrpCatalogPart.objects.count() == 2


def test_history_page_lists_batches(client, make_user, db, admin, settings, tmp_path):
    _login(client, make_user)
    apply_batch(
        run_check(_batch(_workbook([ROW_OK], tmp_path), admin, settings, tmp_path)), by=admin
    )
    html = client.get(reverse("catalog_import_list")).content.decode()
    assert "История импортов" in html
    assert "brp.xlsx" in html


def test_inspector_page_opens(client, make_user, db, admin, settings, tmp_path):
    _login(client, make_user)
    batch = _batch(_workbook([ROW_OK], tmp_path), admin, settings, tmp_path)
    resp = client.get(reverse("catalog_import_inspect", args=[batch.pk]))
    assert resp.status_code == 200
    assert "Material_No" in resp.content.decode()


def test_download_is_protected_and_private(client, make_user, db, admin, settings, tmp_path):
    from apps.accounts import roles

    batch = _batch(_workbook([ROW_OK], tmp_path), admin, settings, tmp_path)
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    assert client.get(reverse("catalog_import_download", args=[batch.pk])).status_code == 403

    client.logout()
    _login(client, make_user)
    response = client.get(reverse("catalog_import_download", args=[batch.pk]))
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    response.close()


@pytest.mark.parametrize("role", ["STOREKEEPER", "SELLER", "VIEWER"])
def test_operator_roles_cannot_import(client, make_user, db, role):
    from apps.accounts import roles

    make_user(f"user-{role}", role=getattr(roles, role))
    client.login(username=f"user-{role}", password=PASSWORD)
    assert client.get(reverse("catalog_import_list")).status_code == 403
    assert client.post(reverse("catalog_import_upload"), {}).status_code == 403


def test_anonymous_is_redirected(client, db):
    resp = client.get(reverse("catalog_import_list"))
    assert resp.status_code in (301, 302)
    assert "/login" in resp["Location"]


# --- Конкуренция двух администраторов --------------------------------------------------------


def test_second_admin_cannot_apply_stale_preview(db, admin, make_user, settings, tmp_path):
    """Два администратора проверили разные файлы, первый применил.

    Второй обязан получить отказ, а не молча затереть результат первого.
    """
    other = make_user("second-admin", is_superuser=True)
    first_file = _workbook([ROW_OK], tmp_path, name="first.xlsx")
    second_file = _workbook([ROW_OBS], tmp_path, name="second.xlsx")

    first = run_check(_batch(first_file, admin, settings, tmp_path))
    second = run_check(_batch(second_file, other, settings, tmp_path))

    apply_batch(first, by=admin)

    with pytest.raises(CatalogImportError) as exc:
        apply_batch(second, by=other)
    assert "STALE_DRY_RUN" in str(exc.value)

    # Результат первого не тронут, второй файл не применён.
    assert BrpCatalogPart.objects.filter(material_no="420831955").exists()
    assert not BrpCatalogPart.objects.filter(material_no="420931284").exists()
    second.refresh_from_db()
    assert second.status == CatalogImportBatch.Status.CHECKED


def test_second_admin_can_apply_after_recheck(db, admin, make_user, settings, tmp_path):
    other = make_user("second-admin", is_superuser=True)
    first = run_check(
        _batch(_workbook([ROW_OK], tmp_path, name="a.xlsx"), admin, settings, tmp_path)
    )
    second = run_check(
        _batch(_workbook([ROW_OBS], tmp_path, name="b.xlsx"), other, settings, tmp_path)
    )
    apply_batch(first, by=admin)

    run_check(second)  # честная повторная проверка на новом состоянии каталога
    apply_batch(second, by=other)
    assert BrpCatalogPart.objects.count() == 2


def test_formula_cells_are_read_as_values(db, admin, settings, tmp_path):
    """Импортёр читает книгу с data_only: формулы не попадают в каталог строкой."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    sheet.append(NOTE_ROW)
    sheet.append(["420831955", "ROLLER", "", "", "25.99", "=20+0", "", ""])
    path = tmp_path / "formula.xlsx"
    workbook.save(path)

    batch = run_check(_batch(path, admin, settings, tmp_path))
    apply_batch(batch, by=admin)
    part = BrpCatalogPart.objects.get()
    # Формула без вычисленного значения не превращается в мусорную цену.
    assert part.wholesale_price_usd is None
    assert part.material_no == "420831955"


def test_blank_material_number_is_skipped(db, admin, settings, tmp_path):
    rows = [["", "NO NUMBER", "", "", "1.00", "0.80", "", ""], ROW_OK]
    batch = run_check(_batch(_workbook(rows, tmp_path), admin, settings, tmp_path))
    apply_batch(batch, by=admin)
    assert BrpCatalogPart.objects.count() == 1
    assert BrpCatalogPart.objects.get().material_no == "420831955"


def test_broken_workbook_is_reported_not_crashed(db, admin, settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    upload = SimpleUploadedFile(
        "broken.xlsx", b"not-a-zip-at-all", content_type="application/vnd.ms-excel"
    )
    batch = save_upload(upload, catalog="brp", by=admin)
    with pytest.raises(CatalogImportError):
        run_check(batch)
    batch.refresh_from_db()
    assert batch.status == CatalogImportBatch.Status.CHECK_FAILED
    assert batch.error_text


def test_failed_check_cannot_be_applied(db, admin, settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    upload = SimpleUploadedFile("broken.xlsx", b"nope", content_type="application/vnd.ms-excel")
    batch = save_upload(upload, catalog="brp", by=admin)
    with pytest.raises(CatalogImportError):
        run_check(batch)
    with pytest.raises(CatalogImportError):
        apply_batch(batch, by=admin)
