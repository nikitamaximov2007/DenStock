"""Cloud adapter behaviour against a fake rclone: accounting, isolation, fail-closed."""

import json
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from apps.operations.backup_budget import BYTE_LIMIT, BudgetError
from apps.operations.dr_remote import (
    RcloneDestination,
    assert_isolated,
    publish_generation,
    validate_remote,
)
from tests.test_dr_local import _keys, _receipt

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class FakeCloud:
    """In-memory rclone: files, S3 hidden versions, Drive trash, multipart leftovers."""

    def __init__(self, kind):
        self.kind = kind
        self.files = {}  # path -> bytes
        self.hidden = 0  # bytes in noncurrent versions / trash
        self.multipart = []
        self.calls = []
        self.fail_upload = False
        self.foreign = 0  # account usage outside the dedicated folder

    def runner(self, args, *, stdin=None, timeout=0):
        self.calls.append(list(args))
        cmd = args[0]
        remote = next(a for a in args if ":" in a and not a.startswith("-"))
        prefix = remote.split(":", 1)[1].strip("/") + "/"
        if cmd == "size":
            if "--drive-trashed-only" in args:
                return json.dumps({"bytes": self.hidden, "sizeless": 0}).encode()
            live = sum(len(v) for k, v in self.files.items() if k.startswith(prefix))
            extra = self.hidden if "--s3-versions" in args else 0
            return json.dumps({"bytes": live + extra, "sizeless": 0}).encode()
        if cmd == "about":
            live = sum(len(v) for v in self.files.values())
            return json.dumps({"used": live + self.hidden + self.foreign}).encode()
        if cmd == "backend" and args[1] == "list-multipart-uploads":
            return json.dumps({"bucket": self.multipart}).encode()
        if cmd == "backend":
            self.multipart = []
            return b"{}"
        if cmd == "lsf":
            names = {k[len(prefix):].split("/")[0] for k in self.files if k.startswith(prefix)}
            return "".join(f"{n}/\n" for n in sorted(names)).encode()
        if cmd == "lsjson":
            base = remote.split(":", 1)[1].strip("/") + "/"
            return json.dumps([
                {"Path": k[len(base):], "Size": len(v), "ModTime": "2026-10-08T11:59:00Z"}
                for k, v in sorted(self.files.items()) if k.startswith(base)
            ]).encode()
        if cmd == "cat":
            return self.files[remote.split(":", 1)[1]]
        if cmd == "deletefile":
            self.files.pop(remote.split(":", 1)[1])
            return b""
        if cmd == "rmdir":
            return b""
        if cmd == "copyto":
            if self.fail_upload:
                raise BudgetError("boom")
            self.files[args[-1].split(":", 1)[1]] = open(args[-2], "rb").read()
            return b""
        if cmd == "rcat":
            self.files[args[-1].split(":", 1)[1]] = stdin
            return b""
        raise AssertionError(args)

    def hasher(self, args, expected):
        data = self.files[args[-1].split(":", 1)[1]]
        if len(data) != expected:
            raise BudgetError("size")
        return sha256(data).hexdigest()


def make(kind, tmp_path, remote, limit=1000, keys=None):
    key, public = keys or _keys(tmp_path)
    cloud = FakeCloud(kind)
    destination = RcloneDestination(
        kind, remote, public, runner=cloud.runner, hasher=cloud.hasher, limit=limit,
        now=lambda: NOW,
    )
    return key, cloud, destination


def put(cloud, key, base, name, size):
    data = b"x" * size
    cloud.files[f"{base}/{name}/bundle.tar.age"] = data
    cloud.files[f"{base}/{name}/receipt.json"] = (
        json.dumps(_receipt(key, name, data), sort_keys=True) + "\n"
    ).encode()


def stage(tmp_path, key, name, size):
    data = b"y" * size
    path = tmp_path / f"stage-{name}"
    path.write_bytes(data)
    return path, _receipt(key, name, data)


def test_remote_must_be_dedicated_namespace():
    for kind, remote in (("s3", "y:bucket"), ("s3", "y:"), ("drive", "g:"), ("drive", "g:../x")):
        with pytest.raises(BudgetError):
            validate_remote(kind, remote)
    assert validate_remote("s3", "y:bucket/dr/") == "y:bucket/dr"


def test_destinations_may_not_overlap():
    assert_isolated(["y:bucket/dr", "g:DenisStock"])
    with pytest.raises(BudgetError):
        assert_isolated(["y:bucket/dr", "y:bucket/dr/sub"])


def test_full_remote_keeps_verified_generations_and_refuses_preupload_rotation(tmp_path):
    key, cloud, dest = make("s3", tmp_path, "y:bucket/dr", limit=1500)
    put(cloud, key, "bucket/dr", "2026-10-01_03-00-00", 300)
    put(cloud, key, "bucket/dr", "2026-10-02_03-00-00", 300)
    cloud.hidden = 100  # noncurrent versions occupy physical space
    bundle, receipt = stage(tmp_path, key, "2026-10-03_03-00-00", 300)
    with pytest.raises(BudgetError, match="не помещается"):
        publish_generation(dest, "2026-10-03_03-00-00", bundle, receipt,
                           allow_best_effort_budget=True)
    assert not any(call[0] in {"deletefile", "rmdir"} for call in cloud.calls)
    assert dest.is_verified("2026-10-01_03-00-00")
    assert dest.is_verified("2026-10-02_03-00-00")
    assert not dest.is_verified("2026-10-03_03-00-00")


def test_hidden_bytes_that_deletion_cannot_free_fail_closed(tmp_path):
    key, cloud, dest = make("drive", tmp_path, "g:DenisStock", limit=1000)
    put(cloud, key, "DenisStock", "2026-10-01_03-00-00", 200)
    cloud.hidden = 700  # trash that the adapter cannot purge
    bundle, receipt = stage(tmp_path, key, "2026-10-02_03-00-00", 300)
    with pytest.raises(BudgetError):
        publish_generation(dest, "2026-10-02_03-00-00", bundle, receipt,
                           allow_best_effort_budget=True)
    assert dest.is_verified("2026-10-01_03-00-00")  # the only good copy survived
    assert "DenisStock/2026-10-02_03-00-00/bundle.tar.age" not in cloud.files


def test_sizeless_or_multipart_leftovers_block_instead_of_guessing(tmp_path):
    key, cloud, dest = make("s3", tmp_path, "y:bucket/dr")
    cloud.multipart = [{"Key": "dr/2026-10-01_03-00-00/bundle.tar.age"}]
    with pytest.raises(BudgetError, match="multipart"):
        dest.physical_bytes()


def test_corrupt_newest_is_never_trusted_as_the_surviving_copy(tmp_path):
    key, cloud, dest = make("s3", tmp_path, "y:bucket/dr", limit=700)
    put(cloud, key, "bucket/dr", "2026-10-01_03-00-00", 300)
    cloud.files["bucket/dr/2026-10-01_03-00-00/bundle.tar.age"] = b"z" * 300  # bit rot
    bundle, receipt = stage(tmp_path, key, "2026-10-02_03-00-00", 300)
    with pytest.raises(BudgetError):
        publish_generation(dest, "2026-10-02_03-00-00", bundle, receipt,
                           allow_best_effort_budget=True)
    assert "bucket/dr/2026-10-01_03-00-00/bundle.tar.age" in cloud.files  # nothing deleted


def test_failed_upload_leaves_previous_generation(tmp_path):
    key, cloud, dest = make("s3", tmp_path, "y:bucket/dr", limit=1000)
    put(cloud, key, "bucket/dr", "2026-10-01_03-00-00", 300)
    cloud.fail_upload = True
    bundle, receipt = stage(tmp_path, key, "2026-10-02_03-00-00", 300)
    with pytest.raises(BudgetError):
        publish_generation(dest, "2026-10-02_03-00-00", bundle, receipt,
                           allow_best_effort_budget=True)
    assert dest.is_verified("2026-10-01_03-00-00")


def test_incomplete_old_upload_is_never_deleted_from_a_single_listing(tmp_path):
    key, cloud, dest = make("s3", tmp_path, "y:bucket/dr")
    put(cloud, key, "bucket/dr", "2026-10-01_03-00-00", 100)
    cloud.files["bucket/dr/2026-10-02_03-00-00/bundle.tar.age"] = b"p" * 50  # no receipt
    cloud.files["bucket/dr/2026-10-02_03-00-00/receipt.json"] = b""
    del cloud.files["bucket/dr/2026-10-02_03-00-00/receipt.json"]
    # fresh (modtime 1 minute ago) partial is kept; old one would be dropped
    assert dest.discard_incomplete() == []
    later = RcloneDestination(
        "s3", "y:bucket/dr", dest.public_key, runner=cloud.runner, hasher=cloud.hasher,
        now=lambda: datetime(2026, 10, 9, tzinfo=UTC),
    )
    assert later.discard_incomplete() == []
    assert "bucket/dr/2026-10-02_03-00-00/bundle.tar.age" in cloud.files
    assert dest.is_verified("2026-10-01_03-00-00")


def test_two_destinations_are_independent(tmp_path):
    key, yandex_cloud, yandex = make("s3", tmp_path, "y:bucket/dr", limit=2500)
    _, drive_cloud, drive = make(
        "drive", tmp_path, "g:DenisStock", limit=2500, keys=(key, yandex.public_key),
    )
    for cloud, base in ((yandex_cloud, "bucket/dr"), (drive_cloud, "DenisStock")):
        put(cloud, key, base, "2026-10-01_03-00-00", 100)
        put(cloud, key, base, "2026-10-02_03-00-00", 100)
    drive_cloud.fail_upload = True
    bundle, receipt = stage(tmp_path, key, "2026-10-03_03-00-00", 300)
    publish_generation(yandex, "2026-10-03_03-00-00", bundle, receipt,
                       allow_best_effort_budget=True)
    with pytest.raises(BudgetError):
        publish_generation(drive, "2026-10-03_03-00-00", bundle, receipt,
                           allow_best_effort_budget=True)
    assert not any("DenisStock" in call[-1] for call in yandex_cloud.calls if call[-1])
    assert not any("bucket/dr" in call[-1] for call in drive_cloud.calls if call[-1])
    assert yandex.is_verified("2026-10-03_03-00-00")
    assert drive.is_verified("2026-10-02_03-00-00")


def test_budget_constant_is_the_contract():
    assert BYTE_LIMIT == 1_000_000_000


def test_drive_account_usage_outside_the_folder_blocks_upload(tmp_path):
    key, cloud, dest = make("drive", tmp_path, "g:DenisStock", limit=1500)
    put(cloud, key, "DenisStock", "2026-10-01_03-00-00", 300)
    cloud.foreign = 1000  # revisions or other data in the account that the folder listing hides
    bundle, receipt = stage(tmp_path, key, "2026-10-02_03-00-00", 300)
    assert dest.physical_bytes() >= 1000
    with pytest.raises(BudgetError):
        publish_generation(dest, "2026-10-02_03-00-00", bundle, receipt,
                           allow_best_effort_budget=True)
    assert dest.is_verified("2026-10-01_03-00-00")


def test_drive_counts_other_google_services_in_the_account_total(tmp_path):
    key, cloud, dest = make("drive", tmp_path, "g:DenisStock", limit=1500)
    real = cloud.runner

    def with_gmail(args, **kw):
        if args[0] == "about":
            return json.dumps({"used": 100, "other": 900}).encode()
        return real(args, **kw)

    dest.runner = with_gmail
    assert dest.physical_bytes() == 1000
