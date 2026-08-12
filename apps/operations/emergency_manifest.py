"""Versioned machine-readable manifests for emergency-capable backups."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .emergency_state import sha256_file

SCHEMA_VERSION = 2
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_SOURCE_ENVIRONMENTS = {"production", "emergency-local", "development", "test"}


class ManifestError(ValueError):
    pass


@dataclass
class ManifestValidation:
    manifest: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors


def _safe_filename(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or Path(value).name != value:
        return None
    return value


def write_manifest(path: str | Path, manifest: dict) -> None:
    path = Path(path)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_manifest(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest.json отсутствует или повреждён") from exc
    if not isinstance(value, dict):
        raise ManifestError("manifest.json должен содержать JSON object")
    return value


def validate_manifest(
    run_dir: str | Path,
    *,
    expected_source: str | None = None,
    require_verified: bool = True,
) -> ManifestValidation:
    """Strict emergency validation with no database or network writes."""
    run_dir = Path(run_dir)
    result = ManifestValidation()
    try:
        result.manifest = read_manifest(run_dir / "manifest.json")
    except ManifestError as exc:
        result.errors.append(str(exc))
        return result
    manifest = result.manifest

    if manifest.get("schema_version") != SCHEMA_VERSION:
        result.errors.append(
            f"неподдерживаемая версия manifest: {manifest.get('schema_version')!r}"
        )
    try:
        uuid.UUID(str(manifest.get("backup_run_id", "")))
    except ValueError:
        result.errors.append("backup_run_id отсутствует или некорректен")
    try:
        created_at = datetime.fromisoformat(str(manifest.get("created_at", "")))
        if created_at.utcoffset() is None:
            raise ValueError
    except ValueError:
        result.errors.append("created_at отсутствует, некорректен или не содержит timezone")

    source = manifest.get("source_environment")
    if source not in ALLOWED_SOURCE_ENVIRONMENTS:
        result.errors.append("source_environment отсутствует или неизвестен")
    if expected_source and source != expected_source:
        result.errors.append(
            f"ожидался source_environment={expected_source}, получен {source or '?'}"
        )
    if require_verified and manifest.get("verification_status") != "verified":
        result.errors.append("backup не помечен как verified")
    if require_verified:
        try:
            verified_at = datetime.fromisoformat(str(manifest.get("verified_at", "")))
            if verified_at.utcoffset() is None:
                raise ValueError
        except ValueError:
            result.errors.append("verified_at отсутствует, некорректен или не содержит timezone")

    source_instance = manifest.get("source_instance_id")
    if (
        not isinstance(source_instance, str)
        or not source_instance.strip()
        or len(source_instance) > 128
    ):
        result.errors.append("source_instance_id отсутствует или некорректен")
    database_name = manifest.get("database_name")
    if not isinstance(database_name, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,128}", database_name
    ):
        result.errors.append("database_name отсутствует или небезопасен")
    storage_origin = manifest.get("storage_origin")
    if (
        not isinstance(storage_origin, str)
        or not storage_origin.strip()
        or len(storage_origin) > 255
    ):
        result.errors.append("storage_origin отсутствует или некорректен")

    for key in ("database_identity", "migration_fingerprint", "media_tree_sha256"):
        value = manifest.get(key)
        if key == "database_identity":
            try:
                uuid.UUID(str(value or ""))
            except ValueError:
                result.errors.append("database_identity отсутствует или некорректен")
        elif not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            result.errors.append(f"{key} отсутствует или не является SHA-256")

    app_commit = manifest.get("app_commit")
    if not isinstance(app_commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", app_commit):
        result.errors.append("app_commit отсутствует или некорректен")

    db_name = _safe_filename(manifest.get("database_dump_filename"))
    db_hash = manifest.get("database_sha256")
    if db_name is None:
        result.errors.append("database_dump_filename отсутствует или небезопасен")
    elif not SHA256_RE.fullmatch(str(db_hash or "")):
        result.errors.append("database_sha256 отсутствует или некорректен")
    else:
        db_path = run_dir / db_name
        if not db_path.is_file():
            result.errors.append(f"файл базы {db_name} отсутствует")
        elif sha256_file(db_path) != db_hash:
            result.errors.append("контрольная сумма базы не совпадает")

    media_name = manifest.get("media_filename")
    media_hash = manifest.get("media_sha256")
    if media_name is not None:
        safe_media = _safe_filename(media_name)
        if safe_media is None:
            result.errors.append("media_filename небезопасен")
        elif not SHA256_RE.fullmatch(str(media_hash or "")):
            result.errors.append("media_sha256 отсутствует или некорректен")
        else:
            media_path = run_dir / safe_media
            if not media_path.is_file():
                result.errors.append(f"архив media {safe_media} отсутствует")
            elif sha256_file(media_path) != media_hash:
                result.errors.append("контрольная сумма media не совпадает")
    elif media_hash is not None:
        result.errors.append("media_sha256 задан без media_filename")

    marker = manifest.get("data_state")
    if not isinstance(marker, dict) or not SHA256_RE.fullmatch(
        str(marker.get("business_sha256", ""))
    ):
        result.errors.append("data_state отсутствует или некорректен")
    else:
        if marker.get("database_identity") != manifest.get("database_identity"):
            result.errors.append("data_state database_identity не совпадает с manifest")
        generation = marker.get("business_generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            result.errors.append("data_state business_generation отсутствует или некорректна")
        if not isinstance(marker.get("tables"), dict):
            result.errors.append("data_state tables отсутствует или некорректен")
        else:
            for table, table_marker in marker["tables"].items():
                if (
                    not isinstance(table, str)
                    or not isinstance(table_marker, dict)
                    or not isinstance(table_marker.get("count"), int)
                    or isinstance(table_marker.get("count"), bool)
                    or table_marker.get("count", -1) < 0
                    or not SHA256_RE.fullmatch(str(table_marker.get("sha256", "")))
                ):
                    result.errors.append(f"data_state table marker некорректен: {table!r}")
                    break

    migration_rows = manifest.get("migration_state")
    if not isinstance(migration_rows, list) or any(
        not isinstance(row, list)
        or len(row) != 2
        or not all(isinstance(value, str) and value for value in row)
        for row in migration_rows or []
    ):
        result.errors.append("migration_state отсутствует или некорректен")
    elif hashlib.sha256(
        json.dumps(
            migration_rows,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest() != manifest.get("migration_fingerprint"):
        result.errors.append("migration_state не совпадает с migration_fingerprint")

    if manifest.get("consistency") not in {"database_snapshot", "single_writer_locked"}:
        result.errors.append("consistency отсутствует или неизвестен")
    return result
