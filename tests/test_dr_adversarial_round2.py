"""Independent Round 2 adversarial cases.

The first block re-states the independent auditor's ten failing cases (all ten
fail on 617305a).  Where the auditor's literal expectation conflicts with the
fail-closed policy, the test asserts the SAFE outcome and says why.  One case
(stale provider accounting) cannot be closed by any client and stays visible as
a strict xfail instead of being hidden.
"""

import json
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

from apps.operations import backup, dr_status
from apps.operations.backup_budget import BudgetError, Generation, prune_superseded
from apps.operations.dr_local import LocalEncryptedStore
from apps.operations.dr_remote import RcloneDestination, publish_generation
from scripts.operations import dr_mac_pull, dr_upload
from tests.test_dr_local import _keys, _receipt
from tests.test_dr_remote import FakeCloud

BASE = "bucket/dr"


def _day(n):
    return f"2026-10-{n:02d}T03:00:00+00:00"


def _name(n):
    return f"2026-10-{n:02d}_03-00-00"


def _cloud(tmp_path, kind="s3", remote="y:bucket/dr", limit=10_000):
    key, public = _keys(tmp_path)
    cloud = FakeCloud(kind)
    dest = RcloneDestination(kind, remote, public, runner=cloud.runner,
                             hasher=cloud.hasher, limit=limit)
    return key, cloud, dest


def _put(cloud, key, n, size=100, base=BASE, body=None):
    data = body or bytes([n]) * size
    cloud.files[f"{base}/{_name(n)}/bundle.tar.age"] = data
    cloud.files[f"{base}/{_name(n)}/receipt.json"] = (
        json.dumps(_receipt(key, _name(n), data, created_at=_day(n)), sort_keys=True) + "\n"
    ).encode()


def _stage(tmp_path, key, n, size=100):
    data = bytes([n + 100]) * size
    path = tmp_path / f"stage-{n}"
    path.write_bytes(data)
    return path, _receipt(key, _name(n), data, created_at=_day(n))


def _publish(dest, tmp_path, key, n, size=100):
    bundle, receipt = _stage(tmp_path, key, n, size)
    return publish_generation(dest, _name(n), bundle, receipt, allow_best_effort_budget=True)


# --- The auditor's ten cases -------------------------------------------------


def test_a01_s3_multipart_cleanup_is_disabled_not_bucket_wide(tmp_path):
    """rclone's cleanup filters by a raw string prefix ("dr" also matches "dr2/"),
    so exact scoping is unprovable: cleanup is disabled, never bucket-wide."""
    key, cloud, dest = _cloud(tmp_path, remote="y:shared-bucket/denstock-only")
    dest.cleanup_multipart()
    assert not [c for c in cloud.calls if c[0] == "backend" and c[1] == "cleanup"]


def test_a01b_foreign_sibling_prefix_upload_is_not_ours_but_ours_blocks(tmp_path):
    key, cloud, dest = _cloud(tmp_path, remote="y:bucket/dr")
    cloud.multipart = [{"Key": "dr2/other-app/file"}]
    dest.physical_bytes()  # a sibling prefix is not counted as ours
    cloud.multipart = [{"Key": "dr/2026-10-01_03-00-00/bundle.tar.age"}]
    with pytest.raises(BudgetError, match="multipart"):
        dest.physical_bytes()


@pytest.mark.parametrize("kind", ["absolute", "relative-escape", "dangling", "nested", "dir"])
def test_a02_a03_symlinks_are_refused_before_anything_is_signed(tmp_path, kind):
    """Auditor wanted either 'not verified' or 'restorable'.  Safe outcome: the
    backup refuses to produce an archive at all, so nothing unrestorable is signed
    and no file outside the tree is ever copied in."""
    private = tmp_path / "private"
    (private / "sub").mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"synthetic")
    link = private / ("sub/a" if kind == "nested" else "a")
    target = {"absolute": outside, "relative-escape": Path("../outside.bin"),
              "dangling": tmp_path / "missing", "nested": outside, "dir": tmp_path}[kind]
    link.symlink_to(target)
    with pytest.raises(backup.OperationsError):
        backup.backup_private_media(tmp_path / "run", private_media_root=private)
    assert not (tmp_path / "run" / "private_media.tar.gz").exists()


def test_a04_mac_falls_back_to_older_verified_remote_generation(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    body = b"older ciphertext"
    receipts = {"generation1": _receipt(key, "generation1", body, created_at=_day(1)),
                "generation2": {"version": 1, "run": "generation2"}}  # corrupt newest
    monkeypatch.setattr(dr_mac_pull, "_rclone", lambda *a: (
        b"generation1/\ngeneration2/\n" if a[0] == "lsf"
        else json.dumps(receipts[a[1].split("/")[-2]]).encode()))
    monkeypatch.setattr(dr_mac_pull, "_download_bounded",
                        lambda s, t, n: Path(t).write_bytes(body))
    assert dr_mac_pull.pull_newest("f:DenisStock", tmp_path / "mac", public) == "generation1"


def test_a05_concurrent_publishers_on_one_host_never_exceed_the_cap(tmp_path):
    key, cloud, dest = _cloud(tmp_path, limit=1500)
    _put(cloud, key, 1, size=300)
    peak = {"bytes": 0}
    real = cloud.runner
    barrier_hit = threading.Event()

    def watching(args, **kw):
        out = real(args, **kw)
        live = sum(len(v) for v in cloud.files.values())
        peak["bytes"] = max(peak["bytes"], live)
        if args[0] == "copyto":
            barrier_hit.set()
        return out

    dest.runner = watching
    twin = RcloneDestination("s3", "y:bucket/dr", dest.public_key, runner=watching,
                             hasher=cloud.hasher, limit=1500)

    def go(d, n):
        try:
            return _publish(d, tmp_path, key, n, size=300)
        except BudgetError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: go(*pair), [(dest, 2), (twin, 3)]))
    assert peak["bytes"] <= 1500
    assert any(isinstance(r, dict) for r in results)


def test_a06_listing_without_receipt_never_causes_deletion(tmp_path):
    key, cloud, dest = _cloud(tmp_path)
    _put(cloud, key, 1)
    del cloud.files[f"{BASE}/{_name(1)}/receipt.json"]  # stale / partial listing
    assert dest.discard_incomplete() == []
    _publish(dest, tmp_path, key, 2)  # prune runs and must skip the unverifiable one
    assert f"{BASE}/{_name(1)}/bundle.tar.age" in cloud.files
    assert not [c for c in cloud.calls if c[0] == "deletefile" and _name(1) in c[-1]]


def test_a07_same_second_backups_get_distinct_dirs_and_keep_the_plain_name(tmp_path,
                                                                          monkeypatch):
    monkeypatch.setattr(backup, "timestamp", lambda: "2026-10-08_03-00-00")
    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda _: backup.new_run_dir(tmp_path), range(16)))
    assert len(set(runs)) == 16
    # The emergency station parses this exact form for the age of a backup.
    assert sum(r.name == "2026-10-08_03-00-00" for r in runs) == 1


def test_a08_years_old_generation_reuploaded_today_is_stale(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    name = "2020-01-01_03-00-00"
    staged = tmp_path / "staging" / name
    staged.mkdir(parents=True)
    body = b"old synthetic backup"
    (staged / "bundle.tar.age").write_bytes(body)
    (staged / "receipt.json").write_text(json.dumps(
        _receipt(key, name, body, created_at="2020-01-01T03:00:00+00:00")))
    monkeypatch.setattr(dr_upload, "publish_generation",
                        lambda *a, **k: {"run": name, "removed": [], "bytes": 30,
                                         "reused": True})
    status = tmp_path / "status.json"
    assert dr_upload.run(staged.parent, ["google=drive=f:DenisStock"], public, status,
                         allow_best_effort_budget=True) == 0
    assert dr_status.problems(status, ["google"], timedelta(hours=36))


def test_a08b_unsigned_or_undated_receipt_is_never_fresh(tmp_path):
    status = tmp_path / "status.json"
    dr_status.record(status, "google", ok=True, run="x", backup_created_at=None)
    assert dr_status.problems(status, ["google"], timedelta(hours=36))
    with pytest.raises(BudgetError):  # an unsigned receipt never reaches a cloud
        staged = tmp_path / "s" / "generation1"
        staged.mkdir(parents=True)
        (staged / "bundle.tar.age").write_bytes(b"x")
        (staged / "receipt.json").write_text("{}")
        dr_upload.run(staged.parent, ["google=drive=f:DenisStock"], _keys(tmp_path)[1],
                      status, allow_best_effort_budget=True)


def test_a09_cloud_publication_is_off_unless_owner_accepts_best_effort_budget(tmp_path):
    key, cloud, dest = _cloud(tmp_path)
    bundle, receipt = _stage(tmp_path, key, 2)
    with pytest.raises(BudgetError, match="не доказан"):
        publish_generation(dest, _name(2), bundle, receipt)
    assert not [c for c in cloud.calls if c[0] in {"copyto", "rcat", "deletefile"}]


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN LIMITATION: a client cannot detect provider accounting that under-reports "
    "existing bytes. Only a provider-enforced quota (Yandex max_size, owner action) "
    "can bound this; Google Drive has none per folder. Not a guarantee we claim."))
def test_a09b_stale_provider_accounting_is_undetectable_by_any_client(tmp_path):
    state = {"actual": 900, "uploaded": False}

    class StaleQuota:
        limit = 1000

        def is_verified(self, name):
            return state["uploaded"]

        def physical_bytes(self):
            return 600  # provider lags by 300 bytes

        def generations(self):
            return [Generation("old", True)]

        def upload(self, name, bundle, receipt_json):
            state["actual"] += bundle.stat().st_size + len(receipt_json)
            state["uploaded"] = True

    bundle = tmp_path / "c.age"
    bundle.write_bytes(b"x" * 300)
    publish_generation(StaleQuota(), "generation2", bundle, {"bytes": 300},
                       allow_best_effort_budget=True)
    assert state["actual"] <= 1000


def test_a10_drive_duplicate_names_or_ids_are_never_verified_or_deleted(tmp_path):
    key, public = _keys(tmp_path)
    receipt = _receipt(key, _name(1), b"cipher", created_at=_day(1))

    def runner(args, **kw):
        if args[0] == "lsjson":
            return json.dumps([
                {"Path": "bundle.tar.age", "Size": 6, "ID": "a"},
                {"Path": "bundle.tar.age", "Size": 6, "ID": "b"},
                {"Path": "receipt.json", "Size": 300, "ID": "c"},
            ]).encode()
        if args[0] == "cat":
            return json.dumps(receipt).encode()
        raise AssertionError(f"must not mutate: {args}")

    dest = RcloneDestination("drive", "g:DenisStock", public, runner=runner,
                             hasher=lambda a, n: sha256(b"cipher").hexdigest())
    assert not dest.is_verified(_name(1))
    with pytest.raises(BudgetError, match="неоднозначен"):
        dest.delete_generation(_name(1))


def test_a10b_duplicate_object_ids_are_ambiguity(tmp_path):
    key, public = _keys(tmp_path)

    def runner(args, **kw):
        return json.dumps([
            {"Path": "bundle.tar.age", "Size": 6, "ID": "same"},
            {"Path": "receipt.json", "Size": 300, "ID": "same"},
        ]).encode()

    dest = RcloneDestination("drive", "g:DenisStock", public, runner=runner)
    with pytest.raises(BudgetError, match="идентификатор"):
        dest.delete_generation(_name(1))


# --- Additional adversarial cases ---------------------------------------------


def test_rotation_happens_only_after_the_new_generation_is_verified(tmp_path):
    key, cloud, dest = _cloud(tmp_path, limit=1500)
    _put(cloud, key, 1, size=300)
    result = _publish(dest, tmp_path, key, 2, size=300)
    assert result["removed"] == [_name(1)]
    order = [c[0] for c in cloud.calls]
    assert order.index("copyto") < order.index("deletefile")
    assert dest.is_verified(_name(2))


def test_no_room_for_old_and_new_together_fails_without_deleting(tmp_path):
    key, cloud, dest = _cloud(tmp_path, limit=900)
    _put(cloud, key, 1, size=300)
    with pytest.raises(BudgetError, match="не помещается"):
        _publish(dest, tmp_path, key, 2, size=300)
    assert dest.is_verified(_name(1))
    assert not [c for c in cloud.calls if c[0] in {"deletefile", "copyto"}]


def test_corrupt_newest_with_valid_older_never_deletes_the_older(tmp_path):
    key, cloud, dest = _cloud(tmp_path)
    _put(cloud, key, 1)
    _put(cloud, key, 2)
    cloud.files[f"{BASE}/{_name(2)}/bundle.tar.age"] = b"rot" * 10  # corrupt newest
    with pytest.raises(BudgetError, match="не подтверждено"):
        prune_superseded(dest, _name(2))
    assert dest.is_verified(_name(1))


def test_newer_signed_generation_is_never_pruned_by_an_older_upload(tmp_path):
    key, cloud, dest = _cloud(tmp_path)
    _put(cloud, key, 5)
    _publish(dest, tmp_path, key, 3)  # an older backup published late
    assert dest.is_verified(_name(5)) and dest.is_verified(_name(3))


def test_interrupted_prune_leaves_an_unverifiable_leftover_that_is_never_auto_deleted(
    tmp_path,
):
    key, cloud, dest = _cloud(tmp_path)
    _put(cloud, key, 1)
    real = cloud.runner

    def crash_on_bundle_delete(args, **kw):
        if args[0] == "deletefile" and args[-1].endswith("bundle.tar.age"):
            raise BudgetError("connection reset")
        return real(args, **kw)

    dest.runner = crash_on_bundle_delete
    with pytest.raises(BudgetError):
        _publish(dest, tmp_path, key, 2)
    dest.runner = real
    assert dest.is_verified(_name(2))  # the new copy is intact
    leftover = f"{BASE}/{_name(1)}/bundle.tar.age"
    assert leftover in cloud.files
    _publish(dest, tmp_path, key, 3)  # next run: leftover still counted, never deleted
    assert leftover in cloud.files
    assert dest.physical_bytes() >= len(cloud.files[leftover])


def test_over_limit_after_upload_is_reported_and_nothing_good_is_deleted(tmp_path):
    key, cloud, dest = _cloud(tmp_path, limit=1300)
    _put(cloud, key, 1, size=300)
    real = cloud.runner

    def provider_overhead(args, **kw):
        out = real(args, **kw)
        if args[0] == "size" and "bucket/dr" in args[-1] and \
                f"{BASE}/{_name(2)}/receipt.json" in cloud.files:
            data = json.loads(out)
            data["bytes"] += 2000  # provider charges more than the listing shows
            return json.dumps(data).encode()
        return out

    dest.runner = provider_overhead
    with pytest.raises(BudgetError, match="выше лимита"):
        _publish(dest, tmp_path, key, 2, size=100)
    assert dest.is_verified(_name(2))


def test_generation_name_collision_with_different_content_is_refused(tmp_path):
    key, cloud, dest = _cloud(tmp_path)
    _put(cloud, key, 2, body=b"original ciphertext")
    real = cloud.runner

    def immutable(args, **kw):
        if args[0] == "copyto" and args[-1].split(":", 1)[1] in cloud.files:
            raise BudgetError("immutable: destination differs")
        return real(args, **kw)

    dest.runner = immutable
    with pytest.raises(BudgetError, match="занято"):
        _publish(dest, tmp_path, key, 2)  # same name, other bytes
    assert cloud.files[f"{BASE}/{_name(2)}/bundle.tar.age"] == b"original ciphertext"


def test_upload_lock_serializes_publish_on_one_host(tmp_path):
    key, cloud, dest = _cloud(tmp_path)
    inside = []
    real = cloud.runner

    def slow(args, **kw):
        if args[0] == "copyto":
            inside.append(threading.get_ident())
            assert len(set(inside)) == 1 or inside.count(inside[-1]) == 1
        return real(args, **kw)

    dest.runner = slow
    twin = RcloneDestination("s3", "y:bucket/dr", dest.public_key, runner=slow,
                             hasher=cloud.hasher, limit=10_000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: _publish(pair[0], tmp_path, key, pair[1]),
                      [(dest, 2), (twin, 3)]))
    assert dest.is_verified(_name(3))


def test_unsafe_archive_members_are_rejected_and_restore_leaves_no_target(tmp_path):
    for member, linkname in (("../escape", None), ("/abs", None), ("./link", "/etc/passwd")):
        archive = tmp_path / "m.tar.gz"
        with tarfile.open(archive, "w:gz") as out:
            info = tarfile.TarInfo(member)
            if linkname:
                info.type, info.linkname = tarfile.SYMTYPE, linkname
                out.addfile(info)
            else:
                info.size = 1
                out.addfile(info, BytesIO(b"x"))
        with pytest.raises(backup.OperationsError):
            backup.verify_media_payload(archive)
        with pytest.raises(backup.OperationsError):
            backup.restore_media(archive, media_root=tmp_path / "target")
        assert not (tmp_path / "target").exists()


def test_mac_keeps_last_good_copy_when_every_remote_is_bad(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public)
    source = tmp_path / "c"
    source.write_bytes(b"existing")
    store.install("generation0", source, _receipt(key, "generation0", b"existing",
                                                  created_at=_day(1)))
    monkeypatch.setattr(dr_mac_pull, "_rclone", lambda *a: (
        b"generation1/\n" if a[0] == "lsf" else b"not json"))
    with pytest.raises(BudgetError):
        dr_mac_pull.pull_newest("f:DenisStock", store.root, public)
    assert store.is_verified("generation0")


def test_status_freshness_uses_signed_creation_time_not_upload_time(tmp_path):
    status = tmp_path / "s.json"
    now = datetime(2026, 10, 9, 4, tzinfo=UTC)
    dr_status.record(status, "yandex", ok=True, run="r",
                     backup_created_at="2026-10-06T03:00:00+00:00", now=now)
    assert dr_status.problems(status, ["yandex"], timedelta(hours=36), now)
    dr_status.record(status, "yandex", ok=True, run="r",
                     backup_created_at="2026-10-09T03:00:00+00:00", now=now)
    assert dr_status.problems(status, ["yandex"], timedelta(hours=36), now) == []
