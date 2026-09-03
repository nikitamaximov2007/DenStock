#!/usr/bin/env python3
"""Read-only-object-store disaster-recovery drill driver.

The command deliberately has no S3 write operation.  It first downloads and
verifies only small manifests, then selects the newest *verified production*
backup and downloads its payload.  Destructive restore operations are possible
only with ``--execute`` and only in a marked disposable directory.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

EXPECTED_FINGERPRINT = "5615837ef355d2d1881508434980efac31f1c467acb3d31c57101ced3ee5d5b1"
PRODUCTION_HOSTS = {"185.250.44.206", "91.142.73.205", "78.17.53.136"}
REQUIRED_MANIFEST_FIELDS = {
    "schema_version",
    "backup_run_id",
    "created_at",
    "source_environment",
    "app_commit",
    "database_dump_filename",
    "database_sha256",
    "media_filename",
    "media_sha256",
    "migration_state",
    "migration_fingerprint",
    "signature",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class DrillError(RuntimeError):
    """A failed-closed recovery-drill precondition."""


def canonical_manifest_payload(manifest: dict[str, Any]) -> bytes:
    payload = dict(manifest)
    payload.pop("signature", None)
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )


def public_key_fingerprint(path: Path) -> str:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise DrillError("Trusted public key is not Ed25519.")
    return hashlib.sha256(
        key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).hexdigest()


def verify_manifest(manifest: dict[str, Any], public_key_path: Path, expected_key_id: str) -> None:
    missing = REQUIRED_MANIFEST_FIELDS - set(manifest)
    if missing:
        raise DrillError("Manifest misses required fields: " + ", ".join(sorted(missing)))
    if manifest.get("schema_version") != 2:
        raise DrillError("Unsupported manifest schema.")
    if manifest.get("source_environment") != "production":
        raise DrillError("Manifest is not a production backup.")
    if not COMMIT_RE.fullmatch(str(manifest.get("app_commit", ""))):
        raise DrillError("Manifest app_commit is not a full SHA.")
    if not SHA256_RE.fullmatch(str(manifest.get("database_sha256", ""))):
        raise DrillError("Manifest database hash is invalid.")
    if not SHA256_RE.fullmatch(str(manifest.get("media_sha256", ""))):
        raise DrillError("Manifest media hash is invalid.")
    if not isinstance(manifest.get("migration_state"), list) or not SHA256_RE.fullmatch(
        str(manifest.get("migration_fingerprint", ""))
    ):
        raise DrillError("Manifest migration metadata is invalid.")
    try:
        uuid.UUID(str(manifest["backup_run_id"]))
        parsed_created_at = dt.datetime.fromisoformat(str(manifest["created_at"]))
        if parsed_created_at.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise DrillError("Manifest run ID or timestamp is invalid.") from exc
    signature = manifest.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        raise DrillError("Manifest signature is missing or unsupported.")
    if signature.get("key_id") != expected_key_id:
        raise DrillError("Manifest signing key ID is not trusted.")
    if public_key_fingerprint(public_key_path) != EXPECTED_FINGERPRINT:
        raise DrillError("Trusted public key fingerprint does not match the pinned production key.")
    try:
        key = serialization.load_pem_public_key(public_key_path.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise TypeError
        key.verify(
            base64.b64decode(signature["value"], validate=True),
            canonical_manifest_payload(manifest),
        )
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise DrillError("Manifest signature is invalid.") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_safe_target(work_dir: Path, *, execute: bool) -> None:
    resolved = work_dir.resolve()
    text = str(resolved).replace("\\", "/").lower()
    if "/opt/denstock" in text or text.rstrip("/").endswith("/denstock"):
        raise DrillError("Recovery target resembles a production checkout.")
    marker = resolved / ".denstock-dr-drill"
    if resolved.exists() and any(resolved.iterdir()) and not marker.is_file():
        raise DrillError(
            "Non-empty recovery target requires the .denstock-dr-drill disposable marker."
        )
    if execute:
        resolved.mkdir(parents=True, exist_ok=True)
        marker.write_text("disposable DR drill only\n", encoding="utf-8")


@dataclass(frozen=True)
class Candidate:
    backup_id: str
    prefix: str
    manifest: dict[str, Any]


def select_latest_verified_backup(
    store: Any, prefix: str, public_key: Path, key_id: str
) -> tuple[Candidate, list[str]]:
    reasons: list[str] = []
    prefixes = sorted(store.list_prefixes(prefix), reverse=True)
    for candidate_prefix in prefixes:
        label = candidate_prefix.rstrip("/").split("/")[-1]
        try:
            manifest = json.loads(store.read_text(candidate_prefix + "manifest.json"))
            verify_manifest(manifest, public_key, key_id)
            names = set(store.list_objects(candidate_prefix))
            required = {
                "manifest.json",
                manifest["database_dump_filename"],
                manifest["media_filename"],
            }
            if not required.issubset(names):
                raise DrillError("required payload is incomplete")
            return Candidate(label, candidate_prefix, manifest), reasons
        except (DrillError, OSError, json.JSONDecodeError) as exc:
            reasons.append(f"{label}: {exc}")
    raise DrillError("No complete verified production backup found. " + "; ".join(reasons))


class S3ReadOnlyStore:
    """Tiny S3 SigV4 client exposing only ListBucket, HeadObject and GetObject."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region

    def _request(self, method: str, key: str = "", query: str = "") -> bytes:
        if method not in {"GET", "HEAD"}:
            raise DrillError("Object-store driver permits only LIST/GET/HEAD.")
        now = dt.datetime.now(dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        canonical_uri = "/" + urllib.parse.quote(self.bucket, safe="")
        if key:
            canonical_uri += "/" + urllib.parse.quote(key, safe="/-_.~")
        canonical_query = query
        host = urllib.parse.urlparse(self.endpoint).netloc
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_headers = (
            f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )

        def sign(key_bytes: bytes, value: str) -> bytes:
            return hmac.new(key_bytes, value.encode(), hashlib.sha256).digest()

        signing_key = sign(
            sign(sign(sign(("AWS4" + self.secret_key).encode(), date_stamp), self.region), "s3"),
            "aws4_request",
        )
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        url = f"{self.endpoint}{canonical_uri}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": authorization,
                "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_hash,
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()

    @staticmethod
    def _canonical_query(parameters: dict[str, str]) -> str:
        """Encode and sort a query exactly as required by AWS SigV4.

        The byte sequence signed here must be the byte sequence sent on the
        wire.  In particular, ``urlencode`` preserves insertion order whereas
        SigV4 requires encoded parameters sorted lexicographically.
        """
        encoded = [
            (
                urllib.parse.quote(str(name), safe="-_.~"),
                urllib.parse.quote(str(value), safe="-_.~"),
            )
            for name, value in parameters.items()
        ]
        return "&".join(f"{name}={value}" for name, value in sorted(encoded))

    def list_prefixes(self, prefix: str) -> list[str]:
        # Backups are flat run directories, therefore one read-only object listing is enough.
        normalized_prefix = prefix.rstrip("/")
        if normalized_prefix:
            normalized_prefix += "/"
        query = self._canonical_query(
            {"list-type": "2", "prefix": normalized_prefix, "delimiter": "/"}
        )
        from xml.etree import ElementTree

        root = ElementTree.fromstring(self._request("GET", query=query))
        return [node.findtext("{*}Prefix") or "" for node in root.findall("{*}CommonPrefixes")]

    def list_objects(self, prefix: str) -> list[str]:
        query = self._canonical_query({"list-type": "2", "prefix": prefix})
        from xml.etree import ElementTree

        root = ElementTree.fromstring(self._request("GET", query=query))
        return [
            (node.findtext("{*}Key") or "").removeprefix(prefix)
            for node in root.findall("{*}Contents")
        ]

    def read_text(self, key: str) -> str:
        return self._request("GET", key).decode("utf-8")

    def download(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._request("GET", key))


def fresh_clone(repo_url: str, commit: str, destination: Path) -> None:
    if destination.exists():
        raise DrillError("Fresh clone target already exists.")
    subprocess.run(
        ["git", "clone", "--no-checkout", "--filter=blob:none", repo_url, str(destination)],
        check=True,
    )
    subprocess.run(["git", "-C", str(destination), "checkout", "--detach", commit], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(destination), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != commit:
        raise DrillError("Fresh clone did not resolve to the signed manifest commit.")


def run_checked(command: list[str], *, cwd: Path) -> str:
    """Run a local disposable-Docker command without including secrets in argv."""
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise DrillError(f"Local DR command could not start ({command[0]}): {exc}") from exc
    if result.returncode:
        raise DrillError(f"Local DR command failed ({command[-1]}): {result.stderr.strip()}")
    return result.stdout.strip()


def write_disposable_env(repo: Path, run_id: str, app_commit: str) -> tuple[str, str]:
    """Create a throwaway compose environment inside the fresh disposable clone."""
    suffix = re.sub(r"[^a-z0-9]", "", run_id.lower())[:12]
    database = f"denstock_dr_{suffix}"
    password = secrets.token_urlsafe(32)
    env = {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_DEBUG": "false",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,web",
        "DJANGO_CSRF_TRUSTED_ORIGINS": "http://localhost,http://127.0.0.1",
        "DJANGO_SECURE_COOKIES": "false",
        "POSTGRES_DB": database,
        "POSTGRES_USER": "denstock_dr",
        "POSTGRES_PASSWORD": password,
        "DATABASE_URL": f"postgres://denstock_dr:{password}@db:5432/{database}",
        "DENSTOCK_MODE": "production",
        "DENSTOCK_PRODUCTION_DB_HOSTS": "db",
        "DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS": "emergency-db",
        "DENSTOCK_APP_COMMIT": app_commit,
        "AI_SUPPORT_ENABLED": "false",
        "AI_SUPPORT_PROVIDER": "disabled",
        "AI_SUPPORT_CODEX_LAUNCH_MODE": "disabled",
    }
    (repo / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8"
    )
    return database, f"denstock-dr-{suffix}"


def write_dr_compose_override(repo: Path) -> Path:
    """Prevent the normal web entrypoint from migrating before DR restore.

    The signed database owns its migration ledger.  Starting the regular
    entrypoint against an empty database first would create schema objects and
    make a subsequent restore collide with them.  This disposable override
    starts gunicorn directly; migration state is compared explicitly after the
    restore instead.
    """
    override = repo / "docker-compose.dr-drill.yml"
    override.write_text(
        "services:\n"
        "  web:\n"
        "    entrypoint: []\n"
        '    command: ["gunicorn", "config.wsgi:application", "--bind",\n'
        '      "0.0.0.0:8000", "--workers", "3", "--timeout", "240"]\n',
        encoding="utf-8",
    )
    return override


def restore_isolated_application(
    repo: Path, backup_dir: Path, candidate: Candidate
) -> dict[str, Any]:
    """Restore only into a new Docker Compose project with no DB port exposure."""
    database, project = write_disposable_env(
        repo, candidate.backup_id, candidate.manifest["app_commit"]
    )
    override = write_dr_compose_override(repo)
    repo_backup = repo / "backups" / candidate.backup_id
    repo_backup.mkdir(parents=True, exist_ok=True)
    for name in (
        "manifest.json",
        candidate.manifest["database_dump_filename"],
        candidate.manifest["media_filename"],
    ):
        shutil.copy2(backup_dir / name, repo_backup / name)
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        "docker-compose.yml",
        "-f",
        override.name,
    ]
    run_checked([*compose, "up", "-d", "--build", "db", "web"], cwd=repo)
    run_checked(
        [
            *compose,
            "exec",
            "-T",
            "web",
            "python",
            "manage.py",
            "verify_backup",
            candidate.backup_id,
        ],
        cwd=repo,
    )
    db_dump = f"/app/backups/{candidate.backup_id}/{candidate.manifest['database_dump_filename']}"
    media = f"/app/backups/{candidate.backup_id}/{candidate.manifest['media_filename']}"
    run_checked(
        [*compose, "exec", "-T", "web", "python", "manage.py", "restore_db", db_dump, "--yes"],
        cwd=repo,
    )
    run_checked(
        [*compose, "exec", "-T", "web", "python", "manage.py", "restore_media", media, "--yes"],
        cwd=repo,
    )
    applied_json = run_checked(
        [
            *compose,
            "exec",
            "-T",
            "web",
            "python",
            "manage.py",
            "shell",
            "-c",
            "from django.db.migrations.recorder import MigrationRecorder; import json; "
            "print(json.dumps(sorted([list(row) for row in "
            "MigrationRecorder.Migration.objects.values_list('app', 'name')])))",
        ],
        cwd=repo,
    )
    try:
        applied = json.loads(applied_json.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise DrillError("Could not read restored migration state.") from exc
    if applied != sorted(candidate.manifest["migration_state"]):
        raise DrillError(
            "Restored migration state differs from signed manifest; no migrate was run."
        )
    migration_state = run_checked(
        [*compose, "exec", "-T", "web", "python", "manage.py", "showmigrations"], cwd=repo
    )
    run_checked([*compose, "exec", "-T", "web", "python", "manage.py", "check"], cwd=repo)
    run_checked([*compose, "exec", "-T", "web", "python", "manage.py", "ops_check"], cwd=repo)
    health = run_checked(
        [
            *compose,
            "exec",
            "-T",
            "web",
            "python",
            "-c",
            "import urllib.request; assert urllib.request.urlopen("
            "'http://localhost:8000/healthz/'"
            ").status == 200",
        ],
        cwd=repo,
    )
    counts = run_checked(
        [
            *compose,
            "exec",
            "-T",
            "web",
            "python",
            "manage.py",
            "shell",
            "-c",
            "from apps.inventory.models import PartItem, StockLot; "
            "from apps.receipts.models import Receipt; "
            "from apps.customers.models import Customer; "
            "from apps.repairs.models import RepairOrder; "
            "print({'parts': PartItem.objects.count(), 'lots': StockLot.objects.count(), "
            "'customers': Customer.objects.count(), 'receipts': Receipt.objects.count(), "
            "'repairs': RepairOrder.objects.count()})",
        ],
        cwd=repo,
    )
    return {
        "compose_project": project,
        "database": database,
        "database_restore_status": "PASS",
        "media_restore_status": "PASS",
        "migration_state": migration_state,
        "migration_state_matches_manifest": "PASS",
        "health_status": "PASS",
        "smoke_counts": counts,
        "docker_db_port_published": "NO",
        "health_output": health,
    }


def write_report(work_dir: Path, report: dict[str, Any]) -> None:
    (work_dir / "dr-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--access-key-env", default="DENSTOCK_DR_S3_ACCESS_KEY")
    parser.add_argument("--secret-key-env", default="DENSTOCK_DR_S3_SECRET_KEY")
    parser.add_argument("--region", default="ru-central1")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--expected-key-id", default="production-1")
    parser.add_argument(
        "--execute", action="store_true", help="Permit disposable local clone/download writes."
    )
    parser.add_argument("--keep-workdir", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    report: dict[str, Any] = {
        "verdict": "FAIL",
        "write_operations_to_bucket": 0,
        "production_target_guard": "PASS",
    }
    try:
        if any(value in args.endpoint for value in PRODUCTION_HOSTS):
            raise DrillError("Object-store endpoint resembles a forbidden production host.")
        ensure_safe_target(args.work_dir, execute=args.execute)
        access_key, secret_key = os.getenv(args.access_key_env), os.getenv(args.secret_key_env)
        if not access_key or not secret_key:
            raise DrillError(
                f"Missing read-only credentials in {args.access_key_env}/{args.secret_key_env}."
            )
        store = S3ReadOnlyStore(args.endpoint, args.bucket, access_key, secret_key, args.region)
        candidate, skipped = select_latest_verified_backup(
            store, args.prefix, args.public_key, args.expected_key_id
        )
        report.update(
            {
                "backup_id": candidate.backup_id,
                "manifest_app_commit": candidate.manifest["app_commit"],
                "signature_status": "PASS",
                "public_key_fingerprint": public_key_fingerprint(args.public_key),
                "skipped_candidates": skipped,
            }
        )
        if not args.execute:
            report["verdict"] = "PRE-RESTORE PASS (rerun with --execute for isolated restore)"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        backup_dir = args.work_dir / "backup"
        for name, expected_hash in (
            (candidate.manifest["database_dump_filename"], candidate.manifest["database_sha256"]),
            (candidate.manifest["media_filename"], candidate.manifest["media_sha256"]),
        ):
            target = backup_dir / name
            store.download(candidate.prefix + name, target)
            if sha256_file(target) != expected_hash:
                raise DrillError(f"Downloaded {name} hash does not match the signed manifest.")
        (backup_dir / "manifest.json").write_text(json.dumps(candidate.manifest), encoding="utf-8")
        fresh_clone(
            "https://github.com/nikitamaximov2007/DenStock.git",
            candidate.manifest["app_commit"],
            args.work_dir / "repo",
        )
        restore_report = restore_isolated_application(args.work_dir / "repo", backup_dir, candidate)
        report.update({"git_head": candidate.manifest["app_commit"], "payload_hashes": "PASS"})
        report.update(restore_report)
        report["verdict"] = "PASS"
        write_report(args.work_dir, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (DrillError, OSError) as exc:
        report["error"] = str(exc)
        if args.work_dir.exists() and (args.work_dir / ".denstock-dr-drill").is_file():
            write_report(args.work_dir, report)
        print(f"DR DRILL FAIL CLOSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
