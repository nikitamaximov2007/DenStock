"""v1.1.5 — import_preset: опциональный импорт стартовых справочников.

Preset наполняет только справочники существующих моделей и НЕ создаёт остатки. Команда
идемпотентна, поддерживает --dry-run (без записи). Модели/миграции/stock-логика не менялись.
"""
import json
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command

from apps.catalog.models import (
    Category,
    Manufacturer,
    PartType,
    VehicleMake,
    VehicleModel,
    VehicleType,
)
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.procurement.models import Batch, BatchLine
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PRESET = "mototech"


def _run(*args) -> str:
    out = StringIO()
    call_command("import_preset", "--preset", PRESET, *args, stdout=out)
    return out.getvalue()


def _directory_counts() -> tuple:
    return (
        VehicleType.objects.count(), VehicleMake.objects.count(), VehicleModel.objects.count(),
        Manufacturer.objects.count(), Category.objects.count(), PartType.objects.count(),
        StorageLocation.objects.count(), Supplier.objects.count(),
    )


# --- dry-run -----------------------------------------------------------------


def test_dry_run_writes_nothing(db):
    before = _directory_counts()
    out = _run("--dry-run")
    assert _directory_counts() == before
    assert not Manufacturer.objects.filter(name="BRP").exists()
    assert not VehicleMake.objects.filter(name="Ski-Doo").exists()
    assert "DRY-RUN" in out


# --- apply -------------------------------------------------------------------


def test_apply_creates_directories(db):
    _run()
    assert Manufacturer.objects.filter(name="BRP").exists()
    assert VehicleType.objects.filter(name="Гидроцикл").exists()
    assert VehicleMake.objects.filter(name="Ski-Doo").exists()
    assert VehicleModel.objects.filter(name="Summit Expert 850").exists()
    assert Category.objects.filter(name="Двигатель").exists()
    assert PartType.objects.filter(name="Двигатель Rotax 900 ACE").exists()
    assert StorageLocation.objects.filter(code="PRESET-ENG").exists()
    assert Supplier.objects.filter(name__icontains="условный").exists()


def test_apply_is_idempotent(db):
    _run()
    counts = _directory_counts()
    _run()
    assert _directory_counts() == counts


def test_runs_on_empty_base(db):
    # На чистой БД (только сиды миграций) команда отрабатывает без ошибок.
    out = _run()
    assert "ПРИМЕНЕНО" in out


# --- НЕ создаёт остатки ------------------------------------------------------


def test_no_stock_objects_created(db):
    _run()
    _run("--dry-run")
    assert StockMovement.objects.count() == 0
    assert StockBalance.objects.count() == 0
    assert StockLot.objects.count() == 0
    assert PartItem.objects.count() == 0
    assert Batch.objects.count() == 0
    assert BatchLine.objects.count() == 0


# --- summary / synonyms ------------------------------------------------------


def test_summary_output(db):
    out = _run()
    assert "part_types" in out
    assert "создано" in out


def test_search_synonyms_reference_only(db):
    path = Path(settings.BASE_DIR) / "data" / "presets" / PRESET / "search_synonyms.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "groups" in data and data["groups"]
    out = _run()
    # Файл упомянут как справочный и не интегрируется в поиск.
    assert "search_synonyms.json" in out
    assert "НЕ интегрируется" in out
