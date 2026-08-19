"""Инструмент базового снимка обязан работать в момент выпуска.

Он запускается ровно дважды за релиз: до развёртывания и после. Если он молча
сломается на переименованном поле, отличить «выпуск не тронул данные» от
«сравнить не удалось» будет нечем, а решение о продолжении принимается именно по
этому сравнению.

Тест исполняет сам файл сценария и проверяет состав вывода.
"""
from __future__ import annotations

import io
import pathlib
import runpy
from contextlib import redirect_stdout

import pytest

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "operations"
    / "release_baseline.py"
)

# Строки, по которым принимается решение продолжать выпуск или остановиться.
REQUIRED_KEYS = [
    "deployed_sha",
    "runtime_sha",
    "mode",
    "database",
    "migrations_total",
    "deployment_state",
    "write_state",
    "business_generation",
    "database_identity",
    "authorized_emergency_primary",
    "primary_authorization_epoch",
    "offline_sessions_unfinished",
    "recount_locks_open",
    "parts_total",
    "brp_total",
    "brp_current",
    "brp_inactive",
    "catalog_import_batches",
    "lots_total",
    "lots_available",
    "stock_available_qty",
    "stock_physical_qty",
    "lots_negative",
    "lots_without_location",
    "lots_without_batch_line",
    "warehouse_actions_total",
    "warehouse_actions_active",
    "sales_total",
    "sales_completed",
    "repairs_total",
    "repairs_completed",
    "reservations_total",
    "customers_total",
]


@pytest.fixture
def baseline_output(db) -> dict[str, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    values = {}
    for line in buffer.getvalue().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def test_the_script_exists_where_the_runbook_says():
    assert SCRIPT.is_file(), "инструкция выпуска ссылается на несуществующий файл"


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_every_decision_key_is_present(baseline_output, key):
    assert key in baseline_output, (
        f"снимок не содержит «{key}»: сравнение до и после станет неполным"
    )


def test_the_snapshot_reports_a_reachable_database(baseline_output):
    assert baseline_output["database"] == "reachable"


def test_the_snapshot_sees_the_control_row(baseline_output):
    """Отсутствие управляющей строки обязано быть видно, а не пропущено."""
    assert baseline_output["deployment_state"] == "present"
    assert baseline_output["write_state"] == "normal"


def test_emergency_starts_unauthorized(baseline_output):
    """Свежая база: аварийный компьютер не назначен, активация закрыта."""
    assert baseline_output["authorized_emergency_primary"] == "none"
    assert baseline_output["primary_authorization_epoch"] == "0"


def test_a_clean_database_has_no_integrity_alarms(baseline_output):
    for key in ("lots_negative", "lots_without_location", "lots_without_batch_line"):
        assert baseline_output[key] == "0", f"{key} должен быть нулём на чистой базе"


def test_the_snapshot_writes_nothing(db):
    """Инструмент запускается на production и обязан быть только читающим."""
    from apps.operations.models import DeploymentState

    before = DeploymentState.objects.get(pk=1).business_generation
    with redirect_stdout(io.StringIO()):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    after = DeploymentState.objects.get(pk=1).business_generation
    assert after == before, "снимок изменил счётчик поколений, значит что-то записал"


def test_the_script_source_contains_no_write_calls():
    """Защита от будущей правки: в сценарии не должно появиться записи."""
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in (".save(", ".create(", ".delete(", ".update(", "get_or_create"):
        assert forbidden not in source, f"в снимок добавлена запись: {forbidden}"
