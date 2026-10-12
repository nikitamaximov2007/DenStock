"""Offline DR cutover: guard, journal, lock and fault-hook rules (no PostgreSQL needed).

The real crash scenarios run in test_dr_cutover_crash.py on PostgreSQL 16.
"""

import inspect
import json
import os

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.urls import get_resolver

from apps.operations import cutover_guard, dr_faults, offline_cutover, offline_restore
from apps.operations.cutover_guard import marker_problems, parse_marker
from apps.operations.offline_cutover import (
    COMMIT_PHRASE,
    CutoverError,
    _classify,
    commit_cutover,
    cutover_state,
    prepare_cutover,
    stage_state,
)
from apps.operations.offline_restore import (
    ExistingJournal,
    Journal,
    JournalCorruptError,
    StageRestoreError,
    dr_operation_lock,
    read_journal,
)

PG = {"ENGINE": "django.db.backends.postgresql", "NAME": "denstock_live"}


# --- fault hook -------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["production", "public-catalog", "emergency-local"])
def test_fault_hook_is_inert_outside_development_and_tests(settings, monkeypatch, mode):
    settings.DENSTOCK_MODE = mode
    settings.DEBUG = True
    monkeypatch.setenv("DENSTOCK_DR_FAULT", "x")
    monkeypatch.setenv("DENSTOCK_DR_FAULT_ACTION", "ENOSPC")
    assert dr_faults.armed() is False
    dr_faults.fault_point("x")  # would raise if armed


def test_fault_hook_needs_debug_in_development(settings, monkeypatch):
    settings.DENSTOCK_MODE = "development"
    settings.DEBUG = False
    monkeypatch.setenv("DENSTOCK_DR_FAULT", "x")
    monkeypatch.setenv("DENSTOCK_DR_FAULT_ACTION", "ENOSPC")
    dr_faults.fault_point("x")
    settings.DEBUG = True
    with pytest.raises(OSError, match="No space"):
        dr_faults.fault_point("x")


def test_fault_hook_fires_only_at_its_named_point(monkeypatch):
    monkeypatch.setenv("DENSTOCK_DR_FAULT", "cutover:after_database_switch")
    monkeypatch.setenv("DENSTOCK_DR_FAULT_ACTION", "ENOSPC")
    dr_faults.fault_point("cutover:after_media_switch")
    with pytest.raises(OSError):
        dr_faults.fault_point("cutover:after_database_switch")


def test_fault_hook_without_variable_does_nothing(monkeypatch):
    monkeypatch.delenv("DENSTOCK_DR_FAULT", raising=False)
    dr_faults.fault_point("journal:verified")


# --- journal ----------------------------------------------------------------------


def _journal(tmp_path, *stages):
    journal = Journal(tmp_path / "j")
    for stage in stages:
        journal.record(stage)
    return journal.path


def test_torn_last_record_is_ignored_and_sealed_before_appending(tmp_path):
    path = _journal(tmp_path, "verified", "prepared")
    with path.open("ab") as handle:
        handle.write(b'{"stage": "switching_datab')
    assert [r["stage"] for r in read_journal(path)] == ["verified", "prepared"]
    ExistingJournal(path).record("recover_started")
    assert [r["stage"] for r in read_journal(path)] == [
        "verified", "prepared", "torn_tail_sealed", "recover_started",
    ]


def test_a_complete_but_unterminated_record_is_still_void(tmp_path):
    path = _journal(tmp_path, "verified")
    with path.open("ab") as handle:
        handle.write(json.dumps({"stage": "database_switched"}).encode())
    assert [r["stage"] for r in read_journal(path)] == ["verified"]
    ExistingJournal(path).record("next")
    assert "database_switched" not in [r["stage"] for r in read_journal(path)]


@pytest.mark.parametrize(
    "garbage",
    [b"\x00garbage{\n", b"[1, 2]\n", b'{"no_stage": 1}\n'],
)
def test_garbage_inside_the_journal_is_corruption(tmp_path, garbage):
    path = _journal(tmp_path, "verified")
    with path.open("ab") as handle:
        handle.write(garbage + b'{"stage": "prepared"}\n')
    with pytest.raises(JournalCorruptError):
        read_journal(path)


def test_a_seal_without_a_preceding_record_is_corruption(tmp_path):
    path = tmp_path / "j"
    path.write_bytes(b'{"stage": "torn_tail_sealed"}\n')
    with pytest.raises(JournalCorruptError):
        read_journal(path)


def test_states_follow_the_last_meaningful_record():
    rows = [{"stage": s} for s in ("validated", "writers_quiesced", "preparing", "prepared")]
    assert cutover_state(rows) == "CUTOVER_PREPARED"
    rows.append({"stage": "recover_started"})
    assert cutover_state(rows) == "CUTOVER_PREPARED"
    rows.append({"stage": "switch_failed"})
    assert cutover_state(rows) == "ROLLBACK_REQUIRED"
    assert stage_state([{"stage": "verified"}, {"stage": "creating_database"}]) == (
        "STAGING_INCOMPLETE"
    )
    assert stage_state([{"stage": "verified"}, {"stage": "staged"}]) == "STAGED"


# --- lock -------------------------------------------------------------------------


def test_second_operation_is_refused_and_holder_survives(tmp_path, settings):
    settings.DENSTOCK_DR_LOCK_PATH = str(tmp_path / "dr.lock")
    with dr_operation_lock(PG, database=False):
        holder = (tmp_path / "dr.lock").read_text()
        assert json.loads(holder)["pid"] == os.getpid()
        with pytest.raises(StageRestoreError, match="Другая DR-операция"):
            with dr_operation_lock(PG, database=False):
                pass
        assert (tmp_path / "dr.lock").read_text() == holder
        assert str(os.getpid()) in offline_restore.lock_holder()
    assert offline_restore.lock_holder() is None
    with dr_operation_lock(PG, database=False):
        pass


# --- generation markers -------------------------------------------------------------


def _marker(**overrides):
    marker = {
        "denstock_dr": 1, "cutover_id": "c1", "generation": "g1", "role": "new",
        "state": "verified", "media_ino": 1, "private_media_ino": 2,
    }
    marker.update(overrides)
    return marker


def test_foreign_or_absent_comment_is_not_a_marker():
    assert parse_marker(None) is None
    assert parse_marker("") is None
    assert parse_marker("обычный комментарий администратора") is None
    assert parse_marker(json.dumps({"other": 1})) is None
    assert parse_marker('{"denstock_dr": broken') == {"invalid": True}


@pytest.mark.parametrize(
    "marker",
    [
        {"invalid": True},
        _marker(denstock_dr=2),
        _marker(role="old", state="cutover"),
        _marker(role="old", state="superseded"),
        _marker(state="cutover"),
        _marker(state="rolled_back"),
    ],
)
def test_unfinished_old_or_rolled_back_generations_are_refused(marker):
    assert marker_problems(marker, mode="production", roots={})


def test_verified_generation_needs_its_own_media_directories(tmp_path):
    media, private = tmp_path / "m", tmp_path / "p"
    media.mkdir()
    private.mkdir()
    roots = {"media": media, "private_media": private}
    marker = _marker(media_ino=media.stat().st_ino, private_media_ino=private.stat().st_ino)
    assert marker_problems(marker, mode="production", roots=roots) == []
    assert marker_problems({**marker, "state": "committed"}, mode="production", roots=roots) == []
    private.rename(tmp_path / "away")
    (tmp_path / "p").mkdir()
    problems = marker_problems(marker, mode="production", roots=roots)
    assert len(problems) == 1 and "private_media" in problems[0]
    # The public runtime has no file stores: only the database state counts.
    assert marker_problems(marker, mode="public-catalog", roots=roots) == []


@pytest.mark.django_db
def test_guard_passes_a_database_without_a_marker():
    """SQLite is never guarded; an unmarked PostgreSQL database passes."""
    if connection.vendor == "sqlite":
        cutover_guard.on_connection_created(connection=connection)
    assert cutover_guard.runtime_problems() == []
    cutover_guard.assert_startup_allowed()


def test_guard_closes_and_refuses_a_marked_postgresql_connection(monkeypatch):
    closed = []

    class Fake:
        vendor = "postgresql"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(cutover_guard, "read_marker", lambda conn: _marker(state="cutover"))
    with pytest.raises(cutover_guard.GenerationRefused, match="запуск запрещён"):
        cutover_guard.on_connection_created(connection=Fake())
    assert closed == [True]
    with cutover_guard.guard_bypass():
        cutover_guard.on_connection_created(connection=Fake())


# --- reality classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "staged", "old", "expected"),
    [
        (10, 20, None, "old"),
        (10, None, None, "old-no-stage"),
        (20, None, 10, "new"),
        (None, 20, 10, "half"),
        (None, None, 10, "unknown"),
        (30, 20, None, "unknown"),
        (20, 20, 10, "unknown"),
        (10, 20, 10, "unknown"),
        (-1, 20, None, "unknown"),
    ],
)
def test_reality_is_classified_or_declared_unknown(live, staged, old, expected):
    assert _classify(live, staged, old, 10, 20) == expected


# --- entry points refuse early ------------------------------------------------------


def test_prepare_requires_postgresql_and_the_writers_flag(tmp_path):
    with pytest.raises(CutoverError, match="только PostgreSQL"):
        prepare_cutover(
            tmp_path / "j", writers_stopped=True,
            settings_dict={"ENGINE": "django.db.backends.sqlite3"},
        )
    with pytest.raises(CutoverError, match="--writers-stopped"):
        prepare_cutover(tmp_path / "j", writers_stopped=False, settings_dict=PG)


def test_commit_requires_the_exact_phrase(tmp_path):
    with pytest.raises(CutoverError, match=COMMIT_PHRASE):
        commit_cutover(tmp_path / "j", post_backup=tmp_path, confirm="да")


@pytest.mark.django_db
def test_status_command_on_a_deployment_without_dr(tmp_path):
    call_command("dr_cutover_status")
    journal = Journal(tmp_path / "stage.journal")
    journal.record("verified")
    journal.record("creating_database", database="x")
    call_command("dr_cutover_status", str(tmp_path / "stage.journal"))


@pytest.mark.django_db
def test_status_command_fails_on_an_unfinished_cutover(tmp_path):
    stage = tmp_path / "stage.journal"
    Journal(stage).record("staged")
    cutover = Journal(offline_cutover.cutover_journal_path(stage))
    cutover.record("validated")
    cutover.record("writers_quiesced")
    cutover.record("preparing", plan=None)
    with pytest.raises(CommandError, match="небезопасен"):
        call_command("dr_cutover_status", str(stage))


def test_discard_is_refused_while_a_cutover_is_not_rolled_back(tmp_path):
    stage = tmp_path / "stage.journal"
    Journal(stage).record("staged")
    Journal(offline_cutover.cutover_journal_path(stage)).record("prepared")
    with pytest.raises(StageRestoreError, match="не откатено"):
        offline_restore.discard_stage(stage, settings_dict=PG)


def test_cutover_is_not_reachable_from_any_url():
    """Views never import the DR tools: no HTTP request can start a cutover."""
    modules = set()

    def walk(patterns):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern.url_patterns)
            elif getattr(pattern, "callback", None) is not None:
                modules.add(inspect.getmodule(pattern.callback))

    walk(get_resolver().url_patterns)
    assert len(modules) > 10
    forbidden = ("offline_cutover", "offline_restore", "dr_cutover", "dr_stage")
    for module in filter(None, modules):
        source = inspect.getsource(module)
        assert not any(name in source for name in forbidden), module.__name__
