#!/usr/bin/env python3
"""Exercise the production retention commands against disposable versioned MinIO buckets.

Requires preinstalled ``minio``, ``mc`` and ``rclone`` binaries. Uses only
synthetic objects and a temporary data directory; it never reads user config.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def _port_pair() -> tuple[int, int]:
    sockets = []
    try:
        for _ in range(2):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return sockets[0].getsockname()[1], sockets[1].getsockname()[1]
    finally:
        for sock in sockets:
            sock.close()


def _run(args: list[str], *, env: dict[str, str]) -> str:
    result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1:] or result.stdout.strip().splitlines()[-1:]
        raise RuntimeError(
            f"{Path(args[0]).name} failed with exit {result.returncode}: "
            f"{detail[0] if detail else 'no diagnostic'}"
        )
    return result.stdout.strip()


def _wait_ready(endpoint: str, process: subprocess.Popen[bytes]) -> None:
    ready = f"{endpoint}/minio/health/ready"
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("disposable MinIO exited before readiness")
        try:
            with urllib.request.urlopen(ready, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError("disposable MinIO did not become ready")


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _version_listing(remote: str, *, env: dict[str, str]) -> str:
    return _run(["rclone", "ls", "--s3-versions", remote], env=env)


def main() -> int:
    binaries = {name: shutil.which(name) for name in ("minio", "mc", "rclone")}
    missing = [name for name, path in binaries.items() if not path]
    if missing:
        raise SystemExit(f"missing preinstalled test binaries: {', '.join(missing)}")

    with tempfile.TemporaryDirectory(prefix="denstock-retention-minio-") as temporary:
        root = Path(temporary)
        data = root / "minio-data"
        data.mkdir(mode=0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        port, console_port = _port_pair()
        endpoint = f"http://127.0.0.1:{port}"
        access_key = "test" + secrets.token_hex(12)
        secret_key = secrets.token_hex(24)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "MC_CONFIG_DIR": str(home / ".mc"),
                "MINIO_ROOT_USER": access_key,
                "MINIO_ROOT_PASSWORD": secret_key,
                "RCLONE_CONFIG": str(root / "empty-rclone.conf"),
                "RCLONE_CONFIG_TESTS3_TYPE": "s3",
                "RCLONE_CONFIG_TESTS3_PROVIDER": "Minio",
                "RCLONE_CONFIG_TESTS3_ACCESS_KEY_ID": access_key,
                "RCLONE_CONFIG_TESTS3_SECRET_ACCESS_KEY": secret_key,
                "RCLONE_CONFIG_TESTS3_ENDPOINT": endpoint,
                "RCLONE_CONFIG_TESTS3_REGION": "us-east-1",
                "RCLONE_CONFIG_TESTS3_FORCE_PATH_STYLE": "true",
                "RCLONE_S3_DISABLE_HTTP2": "true",
            }
        )
        process = subprocess.Popen(
            [
                str(binaries["minio"]),
                "server",
                "--address",
                f"127.0.0.1:{port}",
                "--console-address",
                f"127.0.0.1:{console_port}",
                str(data),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_ready(endpoint, process)
            alias = "retention-test"
            _run(
                [str(binaries["mc"]), "alias", "set", alias, endpoint, access_key, secret_key],
                env=env,
            )
            primary = f"tests3:denstock-primary"
            release = f"tests3:denstock-release-only"
            for bucket in ("denstock-primary", "denstock-release-only"):
                _run([str(binaries["mc"]), "mb", f"{alias}/{bucket}"], env=env)
                target = f"tests3:{bucket}"
                _run(["rclone", "backend", "versioning", target, "Enabled"], env=env)
                actual_versioning = _run(["rclone", "backend", "versioning", target], env=env)
                if actual_versioning.strip('"').lower() != "enabled":
                    raise AssertionError(
                        f"synthetic bucket versioning returned {actual_versioning!r}"
                    )

            sources = root / "source"
            _write(sources / "expired" / "dump.bin", b"old source generation v1")
            _run(
                ["rclone", "copyto", str(sources / "expired/dump.bin"),
                 f"{primary}/2026-10-10_03-00-00/dump.bin"],
                env=env,
            )
            _write(sources / "expired" / "dump.bin", b"old source generation v2")
            _run(
                ["rclone", "copyto", str(sources / "expired/dump.bin"),
                 f"{primary}/2026-10-10_03-00-00/dump.bin"],
                env=env,
            )

            _write(sources / "retained" / "dump.bin", b"current regular generation")
            _run(
                ["rclone", "copyto", str(sources / "retained/dump.bin"),
                 f"{primary}/2026-10-16_03-00-00/dump.bin"],
                env=env,
            )

            _write(sources / "prefix" / "item.bin", b"same-bucket pinned v1")
            _run(
                ["rclone", "copyto", str(sources / "prefix/item.bin"),
                 f"{primary}/release-pinned/PRE/item.bin"],
                env=env,
            )
            _write(sources / "prefix" / "item.bin", b"same-bucket pinned v2")
            _run(
                ["rclone", "copyto", str(sources / "prefix/item.bin"),
                 f"{primary}/release-pinned/PRE/item.bin"],
                env=env,
            )

            protected_sources = []
            for phase in ("PRE", "POST"):
                source_dir = sources / phase
                source = source_dir / f"{phase}.bin"
                _write(source, f"{phase} verified recovery point v1".encode())
                remote_dir = f"{release}/2026-10-16_12-00-0{len(protected_sources)}"
                remote_path = f"{remote_dir}/{phase}.bin"
                _run(["rclone", "copyto", str(source), remote_path], env=env)
                _write(source, f"{phase} verified recovery point v2".encode())
                _run(["rclone", "copyto", str(source), remote_path], env=env)
                protected_sources.append((source_dir, remote_dir))

            release_before = _version_listing(release, env=env)
            protected_version_lines = [
                line for line in release_before.splitlines() if "-v" in line
            ]
            if len(protected_version_lines) != 2:
                raise AssertionError("expected two recoverable historical release versions")

            # Exact deletion operations used by the installed nightly wrapper:
            # purge one selected timestamp run, then clean hidden versions at
            # the standard bucket root.
            _run(["rclone", "purge", f"{primary}/2026-10-10_03-00-00"], env=env)
            _run(["rclone", "backend", "cleanup-hidden", primary], env=env)

            if _version_listing(release, env=env) != release_before:
                raise AssertionError("nightly cleanup changed release-only bucket versions")
            for source_dir, remote_dir in protected_sources:
                _run(["rclone", "check", "--download", str(source_dir), remote_dir], env=env)

            # The root-wide cleanup does erase hidden versions under an unrelated
            # prefix in the same bucket, demonstrating why a prefix is not enough.
            primary_after = _version_listing(primary, env=env)
            if "2026-10-10_03-00-00/" in primary_after:
                raise AssertionError("selected expired generation still exists after purge")
            if "release-pinned/PRE/item-v" in primary_after:
                raise AssertionError("same-bucket hidden version unexpectedly survived cleanup")
            if "2026-10-16_03-00-00/dump.bin" not in primary_after:
                raise AssertionError("retained regular generation was removed")

            # Download one historical protected version and verify its content.
            old_versions = [
                line.split(maxsplit=1)[1]
                for line in protected_version_lines
                if "/PRE-v" in line
            ]
            if not old_versions:
                raise AssertionError(f"could not identify old PRE version: {release_before!r}")
            old_version_key = old_versions[0]
            recovered = root / "recovered-old-pre.bin"
            _run(
                [
                    "rclone",
                    "--s3-versions",
                    "copyto",
                    f"{release}/{old_version_key}",
                    str(recovered),
                ],
                env=env,
            )
            if recovered.read_bytes() != b"PRE verified recovery point v1":
                raise AssertionError("historical protected PRE version did not restore correctly")

            print("MINIO_VERSIONED_RETENTION_ISOLATION_PASS")
            print("nightly purge + bucket-root cleanup-hidden left separate-bucket PRE/POST intact")
            print("same-bucket prefix historical version was removed by cleanup-hidden")
            print("historical PRE version downloaded and SHA/content verified")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
