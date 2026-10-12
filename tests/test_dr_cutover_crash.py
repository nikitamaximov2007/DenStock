"""Offline DR cutover under real crashes on a real PostgreSQL 16 cluster.

Every DR step is a separate ``manage.py`` process.  Fault points
(``DENSTOCK_DR_FAULT``) SIGKILL/SIGTERM that process for real; the next process
starts from whatever is left in the cluster, in the directories and in the
journal.  Assertions are on actual database names/OIDs/comments, directory
inodes and contents, and on what the application does when started.

Needs ``DENSTOCK_TEST_DATABASE_URL`` (any database on the throwaway cluster:
only its host/port/user are used) and PostgreSQL 16 client binaries.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from apps.operations.offline_restore import read_journal
from tests.dr_cutover_support import (
    CUSTOMER_NAME,
    admin,
    build_source,
    cluster_from_env,
    comment,
    databases,
    drop_databases,
    new_deployment,
)

CLUSTER = cluster_from_env()
pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.skipif(CLUSTER is None, reason="Isolated PostgreSQL 16 cluster required"),
]


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    with admin(CLUSTER) as conn:
        version = conn.execute("SHOW server_version_num").fetchone()[0]
    if int(version) // 10000 != 16:
        pytest.skip("Exactly PostgreSQL 16 required")
    prefix = f"drc_{uuid.uuid4().hex[:6]}_"
    root = tmp_path_factory.mktemp("dr-cutover")
    deployment, run = build_source(CLUSTER, root, prefix)
    yield deployment, run, root
    drop_databases(CLUSTER, prefix)


@pytest.fixture
def case(source):
    deployment, run, root = source
    tag = uuid.uuid4().hex[:6]
    return new_deployment(deployment, root / f"case-{tag}", tag), run


# --- helpers ---------------------------------------------------------------------


def stages(path) -> list[str]:
    return [row["stage"] for row in read_journal(path)]


def journal_paths(live, tag="a"):
    stage = live.stage_paths(tag)["journal"]
    return stage, Path(str(stage) + ".cutover")


def status(live, journal=None) -> tuple[int, dict]:
    args = ["dr_cutover_status", "--json"] + ([str(journal)] if journal else [])
    result = live.manage(*args)
    text = result.stdout
    report = json.loads(text[text.index("{"): text.rindex("}") + 1])
    return result.returncode, report


def probe_app(live) -> tuple[int, dict | None, str]:
    result = live.shell("from tests.dr_cutover_support import probe_app; probe_app()")
    data = None
    for line in reversed(result.stdout.strip().splitlines()):
        if line.startswith("{"):
            data = json.loads(line)
            break
    return result.returncode, data, result.output


def assert_startup_refused(live):
    code, data, output = probe_app(live)
    assert code != 0 and data is None
    assert "GenerationRefused" in output and "запуск запрещён" in output
    check = live.manage("check", "--database", "default")
    assert check.returncode != 0 and "operations.E010" in check.output


def assert_old_generation_serves(live):
    """Old DB under the live name, old trees at the live paths, app starts."""
    assert comment(CLUSTER, live.live_db) is None
    assert live.first_name_marker(live.live_db) == "OLD-GENERATION"
    assert (live.media / "old-generation-only.txt").is_file()
    assert (live.private / "old-generation-only.txt").is_file()
    code, data, output = probe_app(live)
    assert code == 0, output
    assert data["first_name"] == "OLD-GENERATION"
    assert data["part_status"] == 200 and data["attachment_ok"] and data["photo_ok"]


def assert_new_generation_serves(live, *, state="verified"):
    marker = comment(CLUSTER, live.live_db)
    assert marker["role"] == "new" and marker["state"] == state
    assert live.first_name_marker(live.live_db) == "BACKUP-GENERATION"
    assert not (live.media / "old-generation-only.txt").exists()
    assert not (live.private / "old-generation-only.txt").exists()
    code, data, output = probe_app(live)
    assert code == 0, output
    assert data == {
        "part_status": 200, "part_has_name": True, "request_status": 200,
        "request_has_customer": True, "attachment_ok": True, "photo_ok": True,
        "first_name": "BACKUP-GENERATION",
    }


def staged_and_prepared(live, run):
    result = live.stage(run)
    assert result.returncode == 0, result.output
    stage_journal, cutover = journal_paths(live)
    result = live.manage("dr_cutover_prepare", str(stage_journal), "--writers-stopped")
    assert result.returncode == 0, result.output
    return stage_journal, cutover


def recover(live, journal, *extra, **kwargs):
    return live.manage("dr_cutover_recover", str(journal), "--writers-stopped", *extra, **kwargs)


def execute(live, journal, **kwargs):
    return live.manage("dr_cutover_execute", str(journal), "--writers-stopped", **kwargs)


def assert_stage_untouched_live(live, stage_journal):
    """A failed or killed staging never touches the live generation."""
    code, report = status(live, stage_journal)
    assert code == 0 and report["safe_to_serve"], report
    assert_old_generation_serves(live)
    refused = live.manage("dr_cutover_prepare", str(stage_journal), "--writers-stopped")
    assert refused.returncode != 0 and "не готов к переключению" in refused.output
    assert not Path(str(stage_journal) + ".cutover").exists()


def discard(live, stage_journal):
    result = live.manage("dr_stage_discard", str(stage_journal), "--yes")
    assert result.returncode == 0, result.output
    return result


# --- 1-3, 12, 13a: crashes and failures while staging ---------------------------


def test_01_sigterm_during_staging_leaves_live_untouched(case):
    live, run = case
    result = live.stage(run, fault="journal:database_restored", action="SIGTERM")
    assert result.returncode == -15
    stage_journal, _ = journal_paths(live)
    assert stages(stage_journal)[-1] == "database_restored"
    assert live.stage_paths()["db"] in databases(CLUSTER, live.prefix)
    code, report = status(live, stage_journal)
    assert report["stage_state"] == "STAGING_INCOMPLETE"
    assert_stage_untouched_live(live, stage_journal)
    discard(live, stage_journal)
    assert live.stage_paths()["db"] not in databases(CLUSTER, live.prefix)


def test_02_sigkill_during_staging_then_discard_and_restage(case):
    live, run = case
    result = live.stage(run, fault="journal:restoring_private_media")
    assert result.returncode == -9
    stage_journal, _ = journal_paths(live)
    assert stages(stage_journal)[-1] == "restoring_private_media"
    assert not live.stage_paths()["private"].exists()
    assert_stage_untouched_live(live, stage_journal)
    discard(live, stage_journal)
    assert not live.stage_paths()["media"].exists()
    again = live.stage(run, tag="b")
    assert again.returncode == 0 and "Итог: staged" in again.output, again.output


@pytest.mark.parametrize(
    ("point", "db_created"),
    [("journal:creating_database", False), ("journal:database_created", True)],
)
def test_03_sigkill_right_after_a_journal_write(case, point, db_created):
    live, run = case
    result = live.stage(run, fault=point)
    assert result.returncode == -9
    stage_journal, _ = journal_paths(live)
    assert stages(stage_journal)[-1] == point.split(":")[1]
    name = live.stage_paths()["db"]
    assert (name in databases(CLUSTER, live.prefix)) is db_created
    assert_stage_untouched_live(live, stage_journal)
    removed = discard(live, stage_journal).output
    assert (f"database:{name}" in removed) is db_created
    assert name not in databases(CLUSTER, live.prefix)


def test_12_partial_media_extraction_never_validates(case):
    live, run = case
    result = live.stage(run, fault="media:after_first_member")
    assert result.returncode == -9
    stage_journal, _ = journal_paths(live)
    assert stages(stage_journal)[-1] == "media_dir_created"
    staged_media = live.stage_paths()["media"]
    assert staged_media.is_dir() and not (staged_media / "part-types" / "dr-photo.jpg").exists()
    assert_stage_untouched_live(live, stage_journal)
    discard(live, stage_journal)
    assert not staged_media.exists()


def test_13a_full_disk_during_staging_is_journalled_and_discardable(case):
    live, run = case
    result = live.stage(run, fault="media:after_first_member", action="ENOSPC")
    assert result.returncode == 1 and "No space left" in result.output
    stage_journal, _ = journal_paths(live)
    last = read_journal(stage_journal)[-1]
    assert last["stage"] == "failed" and last["failed_stage"] == "restoring_media"
    assert_stage_untouched_live(live, stage_journal)
    discard(live, stage_journal)


# --- 9-11: refusals before anything is created -----------------------------------


def test_09_database_restore_failure_is_journalled_live_untouched(source, case):
    deployment, _run, root = source
    live, _ = case
    # A backup whose dump grants to a role that no longer exists at restore time.
    origin = new_deployment(deployment, root / f"origin-{uuid.uuid4().hex[:6]}", "o9")
    role = f"{deployment.prefix}ghost"
    with admin(CLUSTER) as conn:
        conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    with admin(CLUSTER, origin.live_db) as conn:
        conn.execute(sql.SQL("GRANT SELECT ON accounts_user TO {}").format(sql.Identifier(role)))
    assert origin.manage("backup_all", mode="production").returncode == 0
    with admin(CLUSTER, origin.live_db) as conn:
        conn.execute(sql.SQL("REVOKE ALL ON accounts_user FROM {}").format(sql.Identifier(role)))
    with admin(CLUSTER) as conn:
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    run = next(p for p in origin.backups.iterdir() if (p / "manifest.json").exists())

    result = live.stage(run)
    assert result.returncode == 1 and "pg_restore" in result.output
    stage_journal, _ = journal_paths(live)
    last = read_journal(stage_journal)[-1]
    assert last["stage"] == "failed" and last["failed_stage"] == "restoring_database"
    assert_stage_untouched_live(live, stage_journal)
    discard(live, stage_journal)
    assert live.stage_paths()["db"] not in databases(CLUSTER, live.prefix)


def _copy_run(run: Path, target: Path) -> Path:
    shutil.copytree(run, target)
    return target


def test_10_corrupted_manifest_refused_before_and_after_staging(case):
    live, run = case
    tampered = _copy_run(run, live.root / "tampered-run")
    manifest = json.loads((tampered / "manifest.json").read_text())
    manifest["created_at"] = "2001-01-01T00:00:00+00:00"
    (tampered / "manifest.json").write_text(json.dumps(manifest))
    result = live.stage(tampered)
    assert result.returncode == 1 and "Подпись" in result.output
    stage_journal, _ = journal_paths(live)
    assert not stage_journal.exists()
    assert live.stage_paths()["db"] not in databases(CLUSTER, live.prefix)

    # Tampering after a good staging is caught when the cutover re-verifies.
    good = _copy_run(run, live.root / "good-run")
    assert live.stage(good).returncode == 0
    (good / "manifest.json").write_text(json.dumps(manifest))
    refused = live.manage("dr_cutover_prepare", str(stage_journal), "--writers-stopped")
    assert refused.returncode == 1 and "Подпись" in refused.output
    assert not Path(str(stage_journal) + ".cutover").exists()
    assert_old_generation_serves(live)


def test_11_missing_private_archive_refused_before_anything(case):
    live, run = case
    broken = _copy_run(run, live.root / "no-private-run")
    (broken / "private_media.tar.gz").unlink()
    result = live.stage(broken)
    assert result.returncode == 1 and "архив private_media отсутствует" in result.output
    assert not journal_paths(live)[0].exists()
    assert live.stage_paths()["db"] not in databases(CLUSTER, live.prefix)
    assert not live.stage_paths()["private"].exists()


# --- 4-7: crashes during the cutover -----------------------------------------------


def test_04_sigkill_before_the_first_switch_then_forward(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    result = execute(live, stage_journal, fault="cutover:before_database_switch")
    assert result.returncode == -9
    assert stages(cutover)[-1] == "switching_database"
    code, report = status(live, stage_journal)
    assert code == 1 and report["reality"] == {
        "database": "old", "media": "old", "private_media": "old"
    }
    assert_startup_refused(live)
    result = recover(live, stage_journal)
    assert result.returncode == 0 and "POST_CUTOVER_VERIFIED" in result.output, result.output
    assert_new_generation_serves(live)


def test_04b_sigkill_while_stamping_markers_rolls_back(case):
    live, run = case
    live.stage(run)
    stage_journal, cutover = journal_paths(live)
    result = live.manage(
        "dr_cutover_prepare", str(stage_journal), "--writers-stopped",
        fault="cutover:before_markers",
    )
    assert result.returncode == -9
    assert stages(cutover)[-1] == "preparing"
    result = recover(live, stage_journal)
    assert result.returncode == 0 and "ROLLED_BACK" in result.output, result.output
    assert_old_generation_serves(live)
    code, report = status(live, stage_journal)
    assert code == 0 and report["cutover_state"] == "ROLLED_BACK"


def test_05_sigkill_after_database_switch_then_forward(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    result = execute(live, stage_journal, fault="cutover:after_database_switch")
    assert result.returncode == -9
    assert stages(cutover)[-1] == "database_switched"
    found = databases(CLUSTER, live.prefix)
    plan = next(r for r in read_journal(cutover) if r["stage"] == "preparing")["plan"]
    assert found[live.live_db] == plan["database"]["staged_oid"]
    assert found[plan["database"]["old"]] == plan["database"]["live_oid"]
    assert (live.media / "old-generation-only.txt").is_file()  # files still old
    _, report = status(live, stage_journal)
    assert report["reality"] == {"database": "new", "media": "old", "private_media": "old"}
    assert not report["safe_to_serve"]
    assert_startup_refused(live)
    result = recover(live, stage_journal)
    assert result.returncode == 0, result.output
    assert_new_generation_serves(live)


def test_06_sigkill_after_public_media_switch_then_rollback(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    result = execute(live, stage_journal, fault="cutover:after_media_switch")
    assert result.returncode == -9
    _, report = status(live, stage_journal)
    assert report["reality"] == {"database": "new", "media": "new", "private_media": "old"}
    assert_startup_refused(live)
    result = recover(live, stage_journal, "--rollback")
    assert result.returncode == 0 and "ROLLED_BACK" in result.output, result.output
    assert_old_generation_serves(live)
    staged = live.stage_paths()
    assert comment(CLUSTER, staged["db"])["state"] == "rolled_back"
    assert staged["media"].is_dir() and staged["private"].is_dir()
    discard(live, stage_journal)  # allowed only after the rollback
    assert staged["db"] not in databases(CLUSTER, live.prefix)


def test_06b_sigkill_between_the_two_media_renames(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    result = execute(live, stage_journal, fault="cutover:media_half")
    assert result.returncode == -9
    assert not live.media.exists()  # the live path is missing: nothing can serve it
    _, report = status(live, stage_journal)
    assert report["reality"]["media"] == "half" and not report["safe_to_serve"]
    assert_startup_refused(live)
    result = recover(live, stage_journal)
    assert result.returncode == 0, result.output
    assert_new_generation_serves(live)


def test_07_sigkill_after_private_media_switch_then_forward(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    result = execute(live, stage_journal, fault="cutover:after_private_media_switch")
    assert result.returncode == -9
    _, report = status(live, stage_journal)
    assert report["reality"] == {"database": "new", "media": "new", "private_media": "new"}
    assert report["cutover_state"] == "CUTOVER_IN_PROGRESS" and not report["safe_to_serve"]
    assert_startup_refused(live)  # switched but not yet verified: still refused
    result = recover(live, stage_journal)
    assert result.returncode == 0, result.output
    assert_new_generation_serves(live)


# --- 8: interrupted rollback --------------------------------------------------------


@pytest.mark.parametrize("point", ["rollback:after_media", "rollback:media_half"])
def test_08_interrupted_rollback_is_finished_as_a_rollback(case, point):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal).returncode == 0
    result = recover(live, stage_journal, "--rollback", fault=point)
    assert result.returncode == -9
    _, report = status(live, stage_journal)
    assert report["cutover_state"] == "ROLLBACK_IN_PROGRESS" and not report["safe_to_serve"]
    assert report["reality"]["private_media"] == "old"
    assert_startup_refused(live)
    # No --rollback flag: recovery must still finish the rollback, not go forward.
    result = recover(live, stage_journal)
    assert result.returncode == 0 and "ROLLED_BACK" in result.output, result.output
    assert_old_generation_serves(live)


# --- 13b: full disk during the cutover ---------------------------------------------


def test_13b_full_disk_mid_cutover_rolls_back_automatically(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    result = execute(live, stage_journal, fault="cutover:after_media_switch", action="ENOSPC")
    assert result.returncode == 1 and "старое поколение возвращено" in result.output
    rows = stages(cutover)
    assert "switch_failed" in rows and rows[-1] == "rolled_back"
    assert_old_generation_serves(live)


# --- 14: concurrent restore ----------------------------------------------------------


def _wait_for(path: Path, stage: str, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and stage in stages(path):
            return
        time.sleep(0.2)
    raise AssertionError(f"{stage} not reached in {path}")


def test_14_concurrent_restore_is_refused_and_lock_dies_with_the_holder(case):
    live, run = case
    assert live.stage(run).returncode == 0
    stage_journal, cutover = journal_paths(live)
    holder = live.spawn(
        "dr_cutover_prepare", str(stage_journal), "--writers-stopped",
        fault="cutover:before_markers", action="PAUSE", DENSTOCK_DR_FAULT_PAUSE="120",
    )
    try:
        _wait_for(cutover, "preparing")
        for args in (
            ("dr_cutover_recover", str(stage_journal), "--writers-stopped"),
            ("dr_cutover_execute", str(stage_journal), "--writers-stopped"),
        ):
            refused = live.manage(*args)
            assert refused.returncode == 1 and "Другая DR-операция" in refused.output
        other = live.stage(run, tag="b")
        assert other.returncode == 1 and "Другая DR-операция" in other.output
        # A second lock file (another checkout) still meets the cluster-wide lock.
        elsewhere = live.stage(
            run, tag="c", DENSTOCK_DR_LOCK_PATH=str(live.root / "other.lock"),
        )
        assert elsewhere.returncode == 1 and "блокировку в PostgreSQL" in elsewhere.output
        _, report = status(live, stage_journal)
        assert report["lock_holder"] and str(holder.pid) in report["lock_holder"]
    finally:
        holder.kill()
        holder.wait(timeout=30)
    result = recover(live, stage_journal)
    assert result.returncode == 0 and "ROLLED_BACK" in result.output, result.output
    assert_old_generation_serves(live)


# --- 15, 20 and the end-to-end path ------------------------------------------------


def _post_backup(live) -> Path:
    before = {p for p in live.backups.iterdir()} if live.backups.exists() else set()
    result = live.manage("backup_all", mode="production")
    assert result.returncode == 0, result.output
    created = [p for p in live.backups.iterdir() if p not in before and p.is_dir()]
    assert len(created) == 1
    return created[0]


def test_20_successful_final_recovery_commit_and_idempotent_reruns(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal, fault="cutover:after_database_switch").returncode == -9
    result = recover(live, stage_journal)
    assert result.returncode == 0 and "POST_CUTOVER_VERIFIED" in result.output
    assert_new_generation_serves(live)

    post = _post_backup(live)
    wrong = live.manage("dr_cutover_commit", str(stage_journal), "--post-backup", str(run),
                        "--confirm", "ЗАФИКСИРОВАТЬ-DR")
    assert wrong.returncode == 1 and "исходный бэкап" in wrong.output
    result = live.manage("dr_cutover_commit", str(stage_journal), "--post-backup", str(post),
                         "--confirm", "ЗАФИКСИРОВАТЬ-DR")
    assert result.returncode == 0 and "COMMITTED" in result.output, result.output
    assert_new_generation_serves(live, state="committed")
    plan = next(r for r in read_journal(cutover) if r["stage"] == "preparing")["plan"]
    assert comment(CLUSTER, plan["database"]["old"])["state"] == "superseded"
    assert Path(plan["media"]["old"], "old-generation-only.txt").is_file()  # retained

    # 15: repeated runs change nothing.
    before = (stages(cutover), databases(CLUSTER, live.prefix))
    assert "уже завершено" in recover(live, stage_journal).output
    assert execute(live, stage_journal).returncode == 1
    assert live.manage("dr_cutover_prepare", str(stage_journal),
                       "--writers-stopped").returncode == 1
    assert live.stage(run).returncode == 1  # the stage journal already exists
    refused = live.manage("dr_stage_discard", str(stage_journal), "--yes")
    assert refused.returncode == 1 and "не откатено" in refused.output
    code, report = status(live, stage_journal)
    assert code == 0 and report["cutover_state"] == "COMMITTED" and report["safe_to_serve"]
    assert (stages(cutover), databases(CLUSTER, live.prefix)) == before
    assert_new_generation_serves(live, state="committed")


def test_21_retire_marker_after_commit_survives_crash_and_volume_move(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal).returncode == 0
    retire = ("dr_cutover_retire", str(stage_journal), "--confirm")
    early = live.manage(*retire, "СНЯТЬ-МЕТКУ-DR")
    assert early.returncode == 1 and "после фиксации" in early.output
    post = _post_backup(live)
    assert live.manage("dr_cutover_commit", str(stage_journal), "--post-backup", str(post),
                       "--confirm", "ЗАФИКСИРОВАТЬ-DR").returncode == 0
    plan = next(r for r in read_journal(cutover) if r["stage"] == "preparing")["plan"]

    assert live.manage(*retire, "нет").returncode == 1
    killed = live.manage(*retire, "СНЯТЬ-МЕТКУ-DR", fault="retire:after_journal")
    assert killed.returncode == -9
    assert stages(cutover)[-1] == "retiring"
    assert comment(CLUSTER, live.live_db)["state"] == "committed"  # nothing changed yet
    assert "повторите dr_cutover_retire" in recover(live, stage_journal).output

    result = live.manage(*retire, "СНЯТЬ-МЕТКУ-DR")
    assert result.returncode == 0 and "RETIRED" in result.output, result.output
    assert comment(CLUSTER, live.live_db) is None
    assert comment(CLUSTER, plan["database"]["old"])["state"] == "superseded"
    code, report = status(live, stage_journal)
    assert code == 0 and report["cutover_state"] == "RETIRED" and report["safe_to_serve"]
    assert "уже завершено" in recover(live, stage_journal).output
    assert live.manage(*retire, "СНЯТЬ-МЕТКУ-DR").returncode == 1  # not twice

    # A later legitimate volume move (same content, new directory) no longer
    # blocks startup, which a committed marker would (see 18b).
    moved = live.media.with_name(live.media.name + "-moved-away")
    live.media.rename(moved)
    shutil.copytree(moved, live.media)
    code, data, output = probe_app(live)
    assert code == 0 and data["photo_ok"] and data["first_name"] == "BACKUP-GENERATION", output


def test_end_to_end_cutover_then_writes_reopen(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    result = execute(live, stage_journal)
    assert result.returncode == 0, result.output
    assert stages(cutover)[-1] == "post_cutover_verified"
    assert_new_generation_serves(live)
    post = _post_backup(live)
    assert live.manage("dr_cutover_commit", str(stage_journal), "--post-backup", str(post),
                       "--confirm", "ЗАФИКСИРОВАТЬ-DR").returncode == 0
    reopened = live.manage(
        "production_maintenance", "--disable", "--confirm", "ОПАСНО-РАЗРЕШИТЬ-ЗАПИСЬ",
        mode="production",
    )
    assert reopened.returncode == 0, reopened.output
    write = live.shell(
        "from apps.customer_requests.models import CustomerRequest as R; "
        f"r = R.objects.get(customer_name={CUSTOMER_NAME!r}); "
        "r.customer_phone = '1'; r.save(update_fields=['customer_phone']); print('WROTE')",
        mode="production",
    )
    assert write.returncode == 0 and "WROTE" in write.output, write.output
    code, report = status(live, stage_journal)
    assert code == 0 and report["safe_to_serve"]
    assert recover(live, stage_journal).returncode == 0  # idempotent no-op


# --- 16-19: corrupt journal, missing target, wrong generation, restart ------------


def test_16a_torn_last_record_is_sealed_and_ignored(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal, fault="cutover:after_database_switch").returncode == -9
    with cutover.open("ab") as handle:
        handle.write(b'{"stage": "media_swi')  # power loss mid-record
    code, report = status(live, stage_journal)
    assert report["cutover_state"] == "CUTOVER_IN_PROGRESS" and code == 1
    result = recover(live, stage_journal)
    assert result.returncode == 0, result.output
    assert "torn_tail_sealed" in stages(cutover)
    assert_new_generation_serves(live)


def test_16b_garbage_in_the_journal_refuses_recovery_and_changes_nothing(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal, fault="cutover:after_media_switch").returncode == -9
    lines = cutover.read_bytes().split(b"\n")
    lines.insert(2, b"\x00garbage{")
    cutover.write_bytes(b"\n".join(lines))
    before = (databases(CLUSTER, live.prefix), live.media.stat().st_ino)
    result = recover(live, stage_journal)
    assert result.returncode == 1 and "повреждён" in result.output
    code, report = status(live, stage_journal)
    assert code == 1 and report["cutover_state"] == "JOURNAL_CORRUPT"
    assert (databases(CLUSTER, live.prefix), live.media.stat().st_ino) == before
    assert_startup_refused(live)


def test_17_missing_staging_target_rolls_back_never_forward(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    shutil.rmtree(live.stage_paths()["private"])
    refused = execute(live, stage_journal)
    assert refused.returncode == 1 and "dr_cutover_recover" in refused.output
    assert_startup_refused(live)  # prepared: markers are on, nothing switched
    result = recover(live, stage_journal)
    assert result.returncode == 0 and "staging-слот пропал" in result.output, result.output
    assert_old_generation_serves(live)


def test_17b_staging_target_lost_mid_cutover_rolls_back(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal, fault="cutover:after_database_switch").returncode == -9
    shutil.rmtree(live.stage_paths()["media"])
    result = recover(live, stage_journal)
    assert result.returncode == 0 and "ROLLED_BACK" in result.output, result.output
    assert_old_generation_serves(live)


def test_18a_wrong_generation_mid_cutover_is_recovery_required(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal, fault="cutover:after_database_switch").returncode == -9
    staged_media = live.stage_paths()["media"]
    staged_media.rename(staged_media.with_name("moved-away"))
    shutil.copytree(staged_media.with_name("moved-away"), staged_media)  # same bytes, new dir
    before = databases(CLUSTER, live.prefix)
    result = recover(live, stage_journal)
    assert result.returncode == 1 and "ручной разбор" in result.output
    assert stages(cutover)[-1] == "recovery_required"
    assert databases(CLUSTER, live.prefix) == before
    again = recover(live, stage_journal, "--rollback")
    assert again.returncode == 1 and "RECOVERY_REQUIRED" in again.output
    _, report = status(live, stage_journal)
    assert report["cutover_state"] == "RECOVERY_REQUIRED" and not report["safe_to_serve"]
    assert_startup_refused(live)


def test_18b_wrong_media_generation_after_commit_refuses_startup(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal).returncode == 0
    post = _post_backup(live)
    assert live.manage("dr_cutover_commit", str(stage_journal), "--post-backup", str(post),
                       "--confirm", "ЗАФИКСИРОВАТЬ-DR").returncode == 0
    # Someone puts a different tree (even with identical bytes) at the live path.
    live.private.rename(live.private.with_name("swapped-out"))
    shutil.copytree(live.private.with_name("swapped-out"), live.private)
    code, report = status(live)
    assert code == 1 and any("private_media" in p for p in report["problems"])
    assert_startup_refused(live)


def test_19_restart_attempts_with_incomplete_cutover_are_refused(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal, fault="cutover:after_media_switch").returncode == -9
    assert_startup_refused(live)
    migrate = live.manage("migrate", "--noinput", "--skip-checks")
    assert migrate.returncode != 0 and "GenerationRefused" in migrate.output
    for mode in ("production", "public"):  # web/bots settings and catalog-web settings
        refused = live.manage(
            "shell", "-c", "from django.db import connection; connection.ensure_connection()",
            mode=mode,
        )
        assert refused.returncode != 0 and "GenerationRefused" in refused.output, mode
    code, report = status(live)  # no journal given: the runtime verdict alone
    assert code == 1 and not report["safe_to_serve"]
    # Recovery is the only way back; afterwards the application starts.
    assert recover(live, stage_journal).returncode == 0
    assert_new_generation_serves(live)


def test_writers_must_be_proven_stopped(case):
    live, run = case
    assert live.stage(run).returncode == 0
    stage_journal, cutover = journal_paths(live)
    flagless = live.manage("dr_cutover_prepare", str(stage_journal))
    assert flagless.returncode == 1 and "--writers-stopped" in flagless.output
    live.set_write_state(live.live_db, "normal")
    normal = live.manage("dr_cutover_prepare", str(stage_journal), "--writers-stopped")
    assert normal.returncode == 1 and "production_maintenance" in normal.output
    live.set_write_state(live.live_db, "maintenance")
    with psycopg.connect(dbname=live.live_db, autocommit=True, application_name="web-writer",
                         **CLUSTER):
        busy = live.manage("dr_cutover_prepare", str(stage_journal), "--writers-stopped")
    assert busy.returncode == 1 and "web-writer" in busy.output
    assert not cutover.exists()  # refusals leave nothing behind: prepare can be retried
    assert live.manage("dr_cutover_prepare", str(stage_journal),
                       "--writers-stopped").returncode == 0


def test_sigkill_after_proof_before_verified_marker_reproves_forward(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    result = execute(live, stage_journal, fault="cutover:before_verified_marker")
    assert result.returncode == -9
    assert comment(CLUSTER, live.live_db)["state"] == "cutover"
    assert_startup_refused(live)
    result = recover(live, stage_journal)
    assert result.returncode == 0, result.output
    assert stages(cutover).count("verifying") == 2  # proved again, not trusted
    assert_new_generation_serves(live)


def test_sigkill_during_commit_is_finished_by_repeating_commit(case):
    live, run = case
    stage_journal, cutover = staged_and_prepared(live, run)
    assert execute(live, stage_journal).returncode == 0
    post = _post_backup(live)
    args = ("dr_cutover_commit", str(stage_journal), "--post-backup", str(post),
            "--confirm", "ЗАФИКСИРОВАТЬ-DR")
    assert live.manage(*args, fault="journal:committing").returncode == -9
    code, report = status(live, stage_journal)
    assert code == 1 and report["cutover_state"] == "COMMITTING"
    assert "dr_cutover_commit" in recover(live, stage_journal).output
    assert live.manage(*args).returncode == 0
    assert_new_generation_serves(live, state="committed")
    code, report = status(live, stage_journal)
    assert code == 0 and report["cutover_state"] == "COMMITTED"
