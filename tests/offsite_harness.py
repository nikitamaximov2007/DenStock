"""Synthetic offsite harness for denstock-backup-capped-safe.sh.

No cloud, no credentials, no customer data. A fake ``rclone`` emulates a
versioned S3 bucket in a temp directory and a fake ``docker`` emulates
``backup_all`` / ``verify_backup`` with a keyed digest standing in for the
Ed25519 signature. Faults are injected through ``state.json`` so a test can
arm them per run:

* ``copy_fail_after``: stop ``copy`` after N files (partial run left behind);
* ``copy_enospc``: the same, with a "no space left on device" error;
* ``copy_sigterm_after``: send SIGTERM to the wrapper after N files and die,
  like systemd stopping the unit mid-upload;
* ``multipart_cutoff``: a file larger than this that fails mid-upload leaves
  an invisible incomplete multipart upload that the provider still counts;
* ``tamper_local_on_copy``: flip the local db.dump right before upload;
* ``lsf_omit_next``: omit these run names from the next K listings;
* ``cleanup_hidden_fail``: ``backend cleanup-hidden`` fails;
* ``check_fail``: every ``check`` fails.

Provider accounting (what the bucket quota sees) differs from the listing on
purpose: purged bytes stay counted in ``lagged_freed`` until ``settle()``,
like the Yandex accounting lag, and incomplete multipart parts are counted but
never listed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "scripts/operations/denstock-backup-capped-safe.sh"
)
SIGNING_KEY = b"synthetic-signing-key"
TARGET = "fake:bucket/denstock"

FAKE_RCLONE = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, signal, sys

remote = pathlib.Path(os.environ["FAKE_REMOTE"])
state_path = remote / "state.json"
blobs = remote / "blobs"
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as out:
    out.write(json.dumps(args) + "\n")
state = json.loads(state_path.read_text())
faults = state.setdefault("faults", {})


def save():
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(state_path)


def key_of(value):
    prefix = "fake:bucket"
    if not value.startswith(prefix):
        sys.exit(9)
    return value[len(prefix):].strip("/")


def under(key, prefix):
    return not prefix or key == prefix or key.startswith(prefix + "/")


def provider_used():
    return (
        sum(o["size"] for o in state["objects"].values())
        + sum(h["size"] for h in state["hidden"])
        + sum(m["size"] for m in state["multipart"])
        + state["lagged_freed"]
    )


def put(key, data):
    blobs.mkdir(exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    (blobs / digest).write_bytes(data)
    old = state["objects"].get(key)
    if old:
        state["hidden"].append({"key": key, **old})
    state["objects"][key] = {"blob": digest, "size": len(data)}


command = args[0]
if command == "size":
    prefix = key_of(args[2])
    total = sum(o["size"] for k, o in state["objects"].items() if under(k, prefix))
    total += sum(h["size"] for h in state["hidden"] if under(h["key"], prefix))
    print(json.dumps({"bytes": total, "count": 0}))
elif command == "lsf":
    prefix = key_of(args[1])
    names = set()
    for key in state["objects"]:
        if under(key, prefix) and key != prefix:
            rest = key[len(prefix):].lstrip("/") if prefix else key
            if "/" in rest:
                names.add(rest.split("/", 1)[0])
    omit = faults.get("lsf_omit_next")
    if omit and omit["times"] > 0:
        names -= set(omit["names"])
        omit["times"] -= 1
        save()
    print("".join(f"{name}/\n" for name in sorted(names)), end="")
elif command == "copy":
    source = pathlib.Path(args[1])
    dest = key_of(args[2])
    files = sorted(p for p in source.rglob("*") if p.is_file())
    if faults.pop("tamper_local_on_copy", False):
        dump = source / "db.dump"
        dump.write_bytes(b"bitrot" + dump.read_bytes()[6:])
        save()
        files = sorted(p for p in source.rglob("*") if p.is_file())
    for index, path in enumerate(files):
        data = path.read_bytes()
        key = f"{dest}/{path.relative_to(source).as_posix()}"
        stop_after = faults.get("copy_fail_after")
        enospc_after = faults.get("copy_enospc")
        sigterm_after = faults.get("copy_sigterm_after")
        if sigterm_after is not None and index == sigterm_after:
            faults.pop("copy_sigterm_after")
            save()
            os.kill(os.getppid(), signal.SIGTERM)
            sys.exit(143)
        for name, limit, message in (
            ("copy_fail_after", stop_after, "network error"),
            ("copy_enospc", enospc_after, "write: no space left on device"),
        ):
            if limit is not None and index == limit:
                faults.pop(name)
                cutoff = faults.get("multipart_cutoff")
                if cutoff is not None and len(data) > cutoff:
                    state["multipart"].append({"key": key, "size": len(data) // 2})
                save()
                print(f"ERROR : {key}: {message}", file=sys.stderr)
                sys.exit(1)
        quota = state.get("quota")
        if quota is not None and provider_used() + len(data) > quota:
            save()
            print(f"ERROR : {key}: QuotaExceeded", file=sys.stderr)
            sys.exit(1)
        put(key, data)
        save()
elif command == "check":
    if faults.get("check_fail"):
        sys.exit(1)
    # rclone check [--download] SRC DST: flags may come first.
    positional = [a for a in args[1:] if not a.startswith("--")]
    source = pathlib.Path(positional[0])
    dest = key_of(positional[1])
    left = {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*") if p.is_file()
    }
    right = {
        k[len(dest) + 1:]: o["blob"]
        for k, o in state["objects"].items() if under(k, dest) and k != dest
    }
    if left != right:
        print("ERROR : differences found", file=sys.stderr)
        sys.exit(1)
elif command == "purge":
    prefix = key_of(args[1])
    keys = [k for k in state["objects"] if under(k, prefix)]
    if not keys:
        print("ERROR : directory not found", file=sys.stderr)
        sys.exit(3)
    for key in keys:
        state["hidden"].append({"key": key, **state["objects"].pop(key)})
        state["markers"].append(key)
    save()
elif args[:2] == ["backend", "cleanup-hidden"]:
    if faults.get("cleanup_hidden_fail"):
        sys.exit(1)
    prefix = key_of(args[2])
    kept, freed = [], 0
    for item in state["hidden"]:
        if under(item["key"], prefix):
            freed += item["size"]
        else:
            kept.append(item)
    state["hidden"] = kept
    state["lagged_freed"] += freed
    save()
else:
    sys.exit(13)
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, shutil, sys

root = pathlib.Path(os.environ["FAKE_PROJECT"])
key = os.environ["FAKE_SIGNING_KEY"].encode()
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_CALLS"], "a") as out:
    out.write(json.dumps(args) + "\n")


def sign(payload):
    return hashlib.sha256(key + payload).hexdigest()


if "backup_all" in args:
    run_id = os.environ["FAKE_RUN_ID"]
    size = int(os.environ.get("FAKE_RUN_BYTES", "40"))
    run = root / "backups" / run_id
    run.mkdir(parents=True)
    dump = hashlib.sha256(run_id.encode()).digest() * (size // 32 + 1)
    (run / "db.dump").write_bytes(dump[:size])
    body = {"run": run_id, "sha256": {"db.dump": hashlib.sha256(dump[:size]).hexdigest()}}
    payload = json.dumps(body, sort_keys=True).encode()
    (run / "manifest.json").write_text(json.dumps({**body, "signature": sign(payload)}))
    keep = int(args[args.index("--keep-last") + 1])
    runs = sorted(p for p in (root / "backups").iterdir() if p.is_dir())
    for old in runs[:-keep]:
        shutil.rmtree(old)
    print(f"Полный бэкап готов: {run}")
elif "verify_backup" in args:
    run = root / "backups" / args[-1]
    if args[-1] in os.environ.get("FAKE_VERIFY_FAIL", "").split(","):
        sys.exit(2)
    try:
        manifest = json.loads((run / "manifest.json").read_text())
        signature = manifest.pop("signature")
        payload = json.dumps(manifest, sort_keys=True).encode()
        assert signature == sign(payload)
        for name, digest in manifest["sha256"].items():
            assert hashlib.sha256((run / name).read_bytes()).hexdigest() == digest
    except Exception:
        sys.exit(2)
else:
    sys.exit(2)
'''

FLOCK = (
    "#!/usr/bin/env python3\n"
    "import fcntl, sys\n"
    "try:\n"
    "    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "except BlockingIOError:\n"
    "    sys.exit(1)\n"
)


def _signed_run(run: Path, run_id: str, size: int) -> None:
    """Same bytes the fake ``backup_all`` would write for ``run_id``."""
    run.mkdir(parents=True)
    dump = (hashlib.sha256(run_id.encode()).digest() * (size // 32 + 1))[:size]
    (run / "db.dump").write_bytes(dump)
    body = {"run": run_id, "sha256": {"db.dump": hashlib.sha256(dump).hexdigest()}}
    payload = json.dumps(body, sort_keys=True).encode()
    signature = hashlib.sha256(SIGNING_KEY + payload).hexdigest()
    (run / "manifest.json").write_text(json.dumps({**body, "signature": signature}))


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    status: dict


class OffsiteHarness:
    def __init__(self, tmp_path: Path, *, soft_limit: int, quota: int | None = None,
                 run_bytes: int = 40):
        self.tmp = tmp_path
        self.root = tmp_path / "project"
        (self.root / "backups").mkdir(parents=True)
        self.remote = tmp_path / "remote"
        self.remote.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.calls = tmp_path / "rclone-calls.jsonl"
        self.docker_calls = tmp_path / "docker-calls.jsonl"
        self.run_bytes = run_bytes
        for name, body in (("rclone", FAKE_RCLONE), ("docker", FAKE_DOCKER), ("flock", FLOCK)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.write_state({
            "objects": {}, "hidden": [], "markers": [], "multipart": [],
            "lagged_freed": 0, "quota": quota, "faults": {},
        })
        (self.root / ".env.backup").write_text(
            "BACKUP_OFFSITE_ENABLED=true\n"
            f"BACKUP_OFFSITE_TARGET={TARGET}\n"
            f"BACKUP_REMOTE_SOFT_LIMIT_BYTES={soft_limit}\n"
        )

    # --- remote state -----------------------------------------------------
    def state(self) -> dict:
        return json.loads((self.remote / "state.json").read_text())

    def write_state(self, state: dict) -> None:
        (self.remote / "state.json").write_text(json.dumps(state, indent=1, sort_keys=True))

    def arm(self, **faults) -> None:
        state = self.state()
        state["faults"].update(faults)
        self.write_state(state)

    def disarm(self) -> None:
        state = self.state()
        state["faults"] = {}
        self.write_state(state)

    def settle(self) -> None:
        """Provider accounting catches up (the ~20 minute Yandex lag passes)."""
        state = self.state()
        state["lagged_freed"] = 0
        self.write_state(state)

    def provider_used(self) -> int:
        state = self.state()
        return (
            sum(o["size"] for o in state["objects"].values())
            + sum(h["size"] for h in state["hidden"])
            + sum(m["size"] for m in state["multipart"])
            + state["lagged_freed"]
        )

    def remote_runs(self) -> list[str]:
        prefix = TARGET.split(":", 1)[1].split("/", 1)[1] + "/"
        return sorted({
            key[len(prefix):].split("/", 1)[0]
            for key in self.state()["objects"] if key.startswith(prefix)
        })

    def remote_files(self, run_id: str) -> dict[str, str]:
        prefix = f"denstock/{run_id}/"
        return {
            key[len(prefix):]: obj["blob"]
            for key, obj in self.state()["objects"].items() if key.startswith(prefix)
        }

    def restorable_runs(self) -> list[str]:
        """Remote runs whose bytes equal a correctly signed generation."""
        good = []
        for run_id in self.remote_runs():
            reference = self.tmp / "reference" / run_id
            if not reference.exists():
                _signed_run(reference, run_id, self.run_bytes)
            expected = {
                p.relative_to(reference).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in reference.rglob("*") if p.is_file()
            }
            if self.remote_files(run_id) == expected:
                good.append(run_id)
        return good

    def seed_remote(self, run_id: str, *, local: bool = True) -> None:
        """Place a verified generation remotely (and locally, as evidence)."""
        staging = self.tmp / "seed" / run_id
        _signed_run(staging, run_id, self.run_bytes)
        state = self.state()
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                (self.remote / "blobs").mkdir(exist_ok=True)
                (self.remote / "blobs" / digest).write_bytes(data)
                key = f"denstock/{run_id}/{path.relative_to(staging).as_posix()}"
                state["objects"][key] = {"blob": digest, "size": len(data)}
        self.write_state(state)
        if local:
            shutil.copytree(staging, self.root / "backups" / run_id)

    def put_foreign(self, key: str, size: int, *, hidden: bool = False) -> None:
        """An object of another namespace in the same bucket."""
        state = self.state()
        entry = {"blob": "foreign", "size": size}
        if hidden:
            state["hidden"].append({"key": key, **entry})
        else:
            state["objects"][key] = entry
        self.write_state(state)

    # --- running the wrapper ---------------------------------------------
    def env(self, run_id: str) -> dict:
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "DENSTOCK_PROJECT_ROOT": str(self.root),
            "DENSTOCK_BACKUP_LOCK_FILE": str(self.tmp / "backup.lock"),
            "FAKE_PROJECT": str(self.root),
            "FAKE_REMOTE": str(self.remote),
            "FAKE_CALLS": str(self.calls),
            "FAKE_DOCKER_CALLS": str(self.docker_calls),
            "FAKE_SIGNING_KEY": SIGNING_KEY.decode(),
            "FAKE_RUN_ID": run_id,
            "FAKE_RUN_BYTES": str(self.run_bytes),
        }

    def run(self, run_id: str, **extra_env) -> Result:
        self.calls.write_text("")
        self.docker_calls.write_text("")
        completed = subprocess.run(
            ["bash", str(WRAPPER)], cwd=self.root,
            env={**self.env(run_id), **extra_env},
            capture_output=True, text=True, timeout=60,
        )
        status_path = self.root / "backups/offsite_status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        return Result(completed.returncode, completed.stdout, completed.stderr, status)

    def rclone_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def docker_verify_calls(self) -> list[str]:
        return [
            json.loads(line)[-1] for line in self.docker_calls.read_text().splitlines()
            if "verify_backup" in line
        ]
