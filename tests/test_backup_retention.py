"""Удержание резервных копий обязано считать завершённые копии, а не каталоги.

Каталог запуска создаётся В НАЧАЛЕ копирования, поэтому прерванный запуск
оставляет на диске частичный каталог. Пока квота считала каталоги, несколько
сбоев подряд вытесняли исправные копии: на диске оставался мусор вместо запаса.
"""
import json

import pytest

from apps.operations.backup import prune_old_runs


def _complete_run(root, name):
    run = root / name
    run.mkdir(parents=True)
    (run / "db.dump").write_bytes(b"dump")
    (run / "manifest.json").write_text(
        json.dumps({"verification_status": "verified", "backup_run_id": name}),
        encoding="utf-8",
    )
    return run


def _broken_run(root, name, *, manifest=None):
    """Прерванный запуск: каталог есть, пригодного manifest нет."""
    run = root / name
    run.mkdir(parents=True)
    (run / "db.dump.part").write_bytes(b"partial")
    if manifest is not None:
        (run / "manifest.json").write_text(manifest, encoding="utf-8")
    return run


def test_failed_runs_do_not_consume_the_retention_quota(tmp_path):
    """Главный случай: три сбоя подряд не должны съесть запас исправных копий."""
    good = [_complete_run(tmp_path, f"2026-08-0{i}_10-00-00") for i in (1, 2, 3)]
    broken = [_broken_run(tmp_path, f"2026-08-0{i}_10-00-00") for i in (4, 5, 6)]

    prune_old_runs(tmp_path, 3)

    for run in good:
        assert run.exists(), f"удалена исправная копия {run.name}"
    for run in broken:
        assert run.exists(), "каталог новее границы удержания трогать нельзя"


def test_all_complete_runs_behave_exactly_as_before(tmp_path):
    """Когда сбоев нет, поведение прежнее: остаются свежие keep_last."""
    runs = [_complete_run(tmp_path, f"2026-08-{i:02d}_10-00-00") for i in range(1, 6)]

    removed = prune_old_runs(tmp_path, 2)

    assert {p.name for p in removed} == {r.name for r in runs[:3]}
    assert not runs[0].exists() and not runs[2].exists()
    assert runs[3].exists() and runs[4].exists()


def test_partial_runs_are_bounded_and_do_not_pile_up(tmp_path):
    """Мусор ограничен своей квотой, иначе он копился бы вечно."""
    broken = [_broken_run(tmp_path, f"2026-07-0{i}_10-00-00") for i in (1, 2, 3, 4)]
    keep = [_complete_run(tmp_path, f"2026-08-0{i}_10-00-00") for i in (1, 2)]

    removed = prune_old_runs(tmp_path, 2)

    assert {p.name for p in removed} == {broken[0].name, broken[1].name}
    assert all(run.exists() for run in keep), "исправные копии не должны страдать"
    assert broken[2].exists() and broken[3].exists()


def test_directories_without_any_manifest_are_still_capped(tmp_path):
    """Каталоги совсем без manifest тоже подчиняются квоте."""
    runs = [_broken_run(tmp_path, f"2026-0{i}-01_00-00-00") for i in (1, 2, 3)]

    removed = prune_old_runs(tmp_path, 2)

    assert [p.name for p in removed] == [runs[0].name]
    assert runs[1].exists() and runs[2].exists()


def test_unverified_manifest_does_not_count_as_a_backup(tmp_path):
    """Manifest без отметки verified это незавершённая копия."""
    good = _complete_run(tmp_path, "2026-08-01_10-00-00")
    _broken_run(
        tmp_path, "2026-08-02_10-00-00",
        manifest=json.dumps({"verification_status": "failed"}),
    )

    prune_old_runs(tmp_path, 1)

    assert good.exists(), "единственная завершённая копия удалена"


def test_corrupt_manifest_does_not_count_as_a_backup(tmp_path):
    good = _complete_run(tmp_path, "2026-08-01_10-00-00")
    _broken_run(tmp_path, "2026-08-02_10-00-00", manifest="{ not json")

    prune_old_runs(tmp_path, 1)

    assert good.exists()


@pytest.mark.parametrize("keep", [0, -1])
def test_non_positive_keep_deletes_nothing(tmp_path, keep):
    run = _complete_run(tmp_path, "2026-08-01_10-00-00")
    assert prune_old_runs(tmp_path, keep) == []
    assert run.exists()


def test_one_unremovable_run_does_not_block_the_rest_and_is_reported(tmp_path):
    """Production has root-owned historical runs the web UID 2001 cannot delete."""
    import os

    from apps.operations.backup import OperationsError

    runs = [_complete_run(tmp_path, f"2026-08-0{i}_10-00-00") for i in (1, 2, 3, 4)]
    stuck = runs[0]
    os.chmod(stuck, 0o555)  # like a root-owned directory: entries cannot be unlinked
    try:
        with pytest.raises(OperationsError, match=stuck.name):
            prune_old_runs(tmp_path, 2)
        assert stuck.exists() and (stuck / "manifest.json").exists()  # untouched, intact
        assert not runs[1].exists()  # the removable older run still went
        assert runs[2].exists() and runs[3].exists()
    finally:
        os.chmod(stuck, 0o755)


def test_backup_all_survives_a_retention_failure_so_offsite_still_runs(
    tmp_path, db, settings, capsys
):
    """The production wrapper uses `set -e`: a prune error must not abort it."""
    import os
    import sqlite3
    import uuid

    from apps.operations import backup
    from tests.emergency_support import configure_test_trust

    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    root = tmp_path / "backups"
    stuck = _complete_run(root, "2020-01-01_00-00-00")
    _complete_run(root, "2020-01-02_00-00-00")
    database = tmp_path / "src.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
    os.chmod(stuck, 0o555)
    try:
        run = backup.backup_all(
            root=root, keep_last=1, media_root=tmp_path / "no-media",
            private_media_root=tmp_path / "no-private",
            settings_dict={"ENGINE": "django.db.backends.sqlite3", "NAME": str(database)},
        )
    finally:
        os.chmod(stuck, 0o755)
    assert (run / "manifest.json").is_file()
    assert "ротация локальных копий не завершена" in capsys.readouterr().err
    assert stuck.exists()
