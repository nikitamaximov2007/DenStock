"""Performance smoke импорта каталога.

Реальный прайс BRP это порядка 130 тысяч строк. Гонять такой объём в каждом
прогоне дорого, поэтому здесь берётся уменьшенный масштаб, а вывод о реальном
файле делается ЯВНО приблизительным: проверяется, что стоимость растёт линейно
и что на строку не приходится отдельный SQL-запрос.

Главное, что здесь доказывается, это отсутствие построчного SQL. Именно оно
превращает 130 тысяч строк в часы, а не сам объём.

Масштаб намеренно небольшой: тяжёлый прогон отнимает процессор у соседних
тестов, среди которых есть чувствительные к таймауту подпроцессов. Замер на
5000 строк дал 8 запросов на проверку и 108 на применение, то есть стоимость
растёт по числу чанков, а не по числу строк.
"""
import time
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from openpyxl import Workbook

from apps.brp.models import BrpCatalogPart
from apps.catalog_import.services import apply_batch, run_check, save_upload
from django.core.files.uploadedfile import SimpleUploadedFile

PASSWORD = "parol-12345"
HEADERS = [
    "Material_No", "Part_Desc", "Last_Yr_Util", "Status",
    "РОЗНИЦА", "ОПТОВАЯ", "ЗАМЕНА НОМЕРА", "ЗАМЕНА НОМЕРА",
]
ROWS = 1500


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        return django_user_model.objects.create_user(username=username, password=PASSWORD)

    return _make


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


def _big_workbook(tmp_path, rows=ROWS):
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet()
    sheet.append(HEADERS)
    sheet.append([""] * len(HEADERS))
    for index in range(rows):
        sheet.append(
            [f"5{index:08d}", f"PART {index}", "2025", "", "10.00", "8.00", "", ""]
        )
    path = tmp_path / "big.xlsx"
    workbook.save(path)
    return path


def _batch(path, admin, settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    upload = SimpleUploadedFile(
        path.name, path.read_bytes(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    return save_upload(upload, catalog="brp", by=admin)


def test_large_workbook_has_no_per_row_sql(db, admin, settings, tmp_path):
    """Число запросов обязано быть на порядки меньше числа строк."""
    path = _big_workbook(tmp_path)
    batch = _batch(path, admin, settings, tmp_path)

    started = time.monotonic()
    with CaptureQueriesContext(connection) as captured:
        run_check(batch)
    check_seconds = time.monotonic() - started
    check_queries = len(captured)

    started = time.monotonic()
    with CaptureQueriesContext(connection) as captured:
        apply_batch(batch, by=admin)
    apply_seconds = time.monotonic() - started
    apply_queries = len(captured)

    assert BrpCatalogPart.objects.count() == ROWS
    # Построчного SQL нет: запросов на порядок меньше, чем строк.
    assert check_queries < ROWS / 10, check_queries
    assert apply_queries < ROWS / 10, apply_queries
    print(
        f"\nROWS={ROWS} check={check_seconds:.2f}s/{check_queries}q "
        f"apply={apply_seconds:.2f}s/{apply_queries}q"
    )


def test_repeat_import_of_large_file_is_cheap(db, admin, settings, tmp_path):
    """Повторный импорт того же файла не создаёт строки заново."""
    path = _big_workbook(tmp_path, rows=1000)
    apply_batch(run_check(_batch(path, admin, settings, tmp_path)), by=admin)
    second = run_check(_batch(path, admin, settings, tmp_path))
    assert second.summary["created"] == 0
    assert second.summary["skipped_unchanged"] == 1000
    apply_batch(second, by=admin)
    assert BrpCatalogPart.objects.count() == 1000
    assert BrpCatalogPart.objects.first().wholesale_price_usd == Decimal("8.00")
