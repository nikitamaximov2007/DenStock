import json
from pathlib import Path

from apps.operations.dr_local import LocalEncryptedStore
from scripts.operations import dr_mac_pull
from tests.test_dr_local import _keys, _receipt


def test_mac_catches_up_after_offline_and_never_mirrors_deletion(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    remote = {"generation1": b"synthetic-age-one"}

    def fake_rclone(*args):
        if args[0] == "lsf":
            return "".join(f"{name}/\n" for name in remote).encode()
        if args[0] == "cat":
            name = args[1].split("/")[-2]
            return json.dumps(_receipt(key, name, remote[name])).encode()
        raise AssertionError(args)

    monkeypatch.setattr(dr_mac_pull, "_rclone", fake_rclone)
    monkeypatch.setattr(
        dr_mac_pull,
        "_download_bounded",
        lambda source, target, expected: Path(target).write_bytes(remote[source.split("/")[-2]]),
    )
    root = tmp_path / "mac"
    assert dr_mac_pull.pull_newest("google:DenisStock", root, public) == "generation1"
    remote["generation2"] = b"synthetic-age-two"
    assert dr_mac_pull.pull_newest("google:DenisStock", root, public) == "generation2"
    del remote["generation1"]
    assert dr_mac_pull.pull_newest("google:DenisStock", root, public) == "generation2"
    store = LocalEncryptedStore(root, public)
    assert store.is_verified("generation1")
    assert store.is_verified("generation2")
