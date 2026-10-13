#!/usr/bin/env python3
"""Real-service acceptance of the GitHub backup channel with HARMLESS data.

    GHB_REPO=<owner>/<backup repo> GHB_TOKEN_FILE=<write token file> \\
        python3 scripts/operations/github_backup_acceptance.py [--source-repo owner/DenStock]

Run by the owner on the Mac, once, on the NEW EMPTY backup repository, before
the daily job is enabled. Nothing here touches production: the backup is a
synthetic run of random bytes, encrypted to a throwaway age identity generated
for this test (so the read-back can be decrypted without the owner's
passphrase). It proves, against real GitHub:

 1. private repository access, and that the token cannot see the source repo;
 2. draft creation, encrypted upload to the upload host, read-back by SHA-256
    (the download redirect is followed WITHOUT the token), publication;
 3. listing and byte accounting of the published generation;
 4. FRESH/DEGRADED monitoring and a deep hash check;
 5. fetch + age decryption back to the exact synthetic bytes;
 6. deletion of ONLY the release this test created (by id), then an empty repo.

It refuses to run if the repository already has releases.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import github_release_backup as ghb  # noqa: E402

MARKER = b"DENISSTOCK-ACCEPTANCE-PLAINTEXT"


def step(message):
    print(f"[acceptance] {message}", flush=True)


def synthetic_run(root: Path, key) -> str:
    when = datetime.now(timezone.utc).replace(microsecond=0)  # noqa: UP017 - runs on macOS 3.9
    run = when.strftime("%Y-%m-%d_%H-%M-%S")
    folder = root / run
    folder.mkdir()
    hashes = {}
    for name, size in (("db.dump", 600_000), ("media.tar.gz", 100_000),
                       ("private_media.tar.gz", 300_000)):
        data = MARKER + os.urandom(size)
        (folder / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    manifest = {
        "verification_status": "verified", "created_at": when.isoformat(),
        "app_commit": "0" * 40, "private_media_status": "included",
        "private_media_filename": "private_media.tar.gz", "private_media_file_count": 1,
        "media_filename": "media.tar.gz", "sha256": hashes}
    payload = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    signature = base64.b64encode(key.sign(payload.encode("ascii"))).decode()
    manifest["signature"] = {"algorithm": "ed25519", "key_id": "acceptance", "value": signature}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return run


def main() -> int:
    source_repo = ""
    if "--source-repo" in sys.argv:
        source_repo = sys.argv[sys.argv.index("--source-repo") + 1]
    conf = ghb.load_config()
    gh = ghb.GitHub(conf, ghb.read_token(conf))
    step(f"1. repository {gh.repo}")
    repo = gh.repository()
    assert repo.get("private") is True, "repository is not private"
    if source_repo:
        try:
            gh.call("GET", f"/repos/{source_repo}", what="source repo")
            raise SystemExit("STOP: the token can read the source repository: scope it to the "
                             "backup repository only")
        except ghb.BackupError:
            step("   token cannot see the source repository: OK")
    if gh.releases():
        raise SystemExit("STOP: the repository already has releases; run on an empty one")

    with tempfile.TemporaryDirectory(prefix="ghb-acceptance-") as tmp:
        tmp = Path(tmp)
        identity, recipient = tmp / "identity.txt", tmp / "recipient.txt"
        subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
        recipient.write_text(subprocess.run(["age-keygen", "-y", str(identity)], check=True,
                                            capture_output=True, text=True).stdout)
        backups = tmp / "backups"
        backups.mkdir()
        # A throwaway signing key: the job verifies Ed25519 before upload.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        pubkey = tmp / "acceptance-signing.pub"
        pubkey.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        run = synthetic_run(backups, key)
        env = {**conf, "BACKUP_ROOT": str(backups), "GHB_RECIPIENT_FILE": str(recipient),
               "GHB_MANIFEST_PUBKEY": str(pubkey),
               "GHB_LOCK_FILE": str(tmp / "lock"), "GHB_STATUS_FILE": str(tmp / "status.json")}

        step("2. push: draft, upload, read-back, publish")
        if ghb.push(env) != 0:
            raise SystemExit("STOP: push failed: " + (tmp / "status.json").read_text())
        status = json.loads((tmp / "status.json").read_text())
        step(f"   {status['message']}")

        step("3. listing and accounting")
        gens = ghb.inventory(gh)
        mine = [g for g in gens if g["run"] == run]
        assert len(mine) == 1 and mine[0]["kind"] == "valid", gens
        assert ghb.total_bytes(gens) == mine[0]["bytes"], "unexpected bytes in the repository"
        step(f"   generation {mine[0]['tag']} valid, {mine[0]['bytes']} bytes")

        step("3b. pagination as the job relies on it (per_page=1)")
        first = gh.call("GET", f"/repos/{gh.repo}/releases?per_page=1&page=1", what="page 1")
        second = gh.call("GET", f"/repos/{gh.repo}/releases?per_page=1&page=2", what="page 2")
        assert len(first) == 1 and second == [], (len(first), len(second))
        assets = gh.call("GET", f"/repos/{gh.repo}/releases/{mine[0]['id']}/assets?per_page=1",
                         what="assets page")
        assert len(assets) == 1, "asset pagination did not honour per_page"

        step("4. deep check")
        args = type("A", (), {"deep": True, "deep_all": True, "max_age_hours": 26})()
        assert ghb.check(env, args) in (0, 2), "deep check failed"  # 2 = DEGRADED (one copy)

        step("5. fetch and decrypt")
        out = tmp / "fetched"
        fargs = type("A", (), {"out": str(out), "tag": mine[0]["tag"]})()
        assert ghb.fetch(env, fargs) == 0
        plain = tmp / "plain"
        plain.mkdir()
        cipher = out / f"{run}.signed-backup.tar.age"
        assert MARKER not in cipher.read_bytes(), "plaintext found in the uploaded asset"
        dec = subprocess.Popen(["age", "-d", "-i", str(identity), str(cipher)],
                               stdout=subprocess.PIPE)
        subprocess.run(["tar", "-xf", "-", "-C", str(plain)], stdin=dec.stdout, check=True)
        assert dec.wait() == 0
        for name in ("db.dump", "media.tar.gz", "private_media.tar.gz", "manifest.json"):
            assert (plain / run / name).read_bytes() == (backups / run / name).read_bytes(), name
        step("   decrypted bytes equal the synthetic originals")

        step(f"6. delete ONLY the release created by this test: {mine[0]['tag']}")
        if "--confirm-delete" not in sys.argv:
            answer = input("Type DELETE to remove this one test release (anything else keeps it): ")
            if answer.strip() != "DELETE":
                step("KEPT. Delete this test release in the GitHub UI before enabling the "
                     "daily job: until then it looks like a valid generation.")
                return 2
        gh.delete_release(mine[0]["id"])
        assert not [g for g in ghb.inventory(gh) if g["run"] == run]
    step("ACCEPTED: the real GitHub service behaves as the job expects. "
         "The git tag of the test release stays (deleting a release keeps its tag).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
