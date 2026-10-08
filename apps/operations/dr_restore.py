"""All-or-nothing restore of an age bundle into a NEW database and NEW media dirs.

Nothing existing is overwritten: the target database and both media directories
must not exist.  On any failure the new database, staged directories and every
plaintext temporary file are removed, so a failed drill leaves no half state.
The operator performs the cut-over (point the app at the new database/dirs)
only after this returns.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .dr_local import sha256_file, verify_receipt
from .emergency_state import media_tree_sha256

DB_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
ALLOWED = {"manifest.json", "db.dump", "media.tar.gz", "private_media.tar.gz"}


class RestoreError(RuntimeError):
    pass


@dataclass
class RestoreReport:
    database: str
    media: Path
    private_media: Path
    seconds: float
    checks: list[str]


def _extract_bundle(bundle: Path, identity: Path, work: Path, max_bytes: int) -> None:
    process = subprocess.Popen(
        ["age", "-d", "-i", str(identity), str(bundle)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    total = 0
    try:
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if member.name not in ALLOWED or not member.isfile():
                    raise RestoreError("Недопустимый элемент в зашифрованном архиве.")
                total += member.size
                if total > max_bytes:
                    raise RestoreError("Архив превышает допустимый объём распаковки.")
                source = archive.extractfile(member)
                with (work / member.name).open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        process.stdout.read()  # drain so age can finish and authenticate the tail
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError("Не удалось расшифровать или распаковать архив.") from exc
    finally:
        if process.stdout:
            process.stdout.close()
        code = process.wait()
    if code != 0:
        raise RestoreError("Расшифровка age не удалась (ключ неверен или архив повреждён).")


def _verify_contents(work: Path, public_key: Path, key_id: str) -> dict:
    try:
        manifest = json.loads((work / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise RestoreError("manifest.json отсутствует или повреждён.") from exc
    if not verify_receipt(manifest, public_key, key_id):
        raise RestoreError("Подпись manifest не прошла проверку.")
    checksums = manifest.get("sha256") or {}
    for required in ("db.dump", "private_media.tar.gz"):
        if required not in checksums or not (work / required).is_file():
            raise RestoreError(f"В бэкапе отсутствует {required}.")
    for name, expected in checksums.items():
        if name not in ALLOWED or sha256_file(work / name) != expected:
            raise RestoreError(f"Контрольная сумма {name} не совпала.")
    return manifest


def _stage_media(archive: Path, parent: Path, expected_tree: str) -> Path:
    staged = parent / f".restore-{secrets.token_hex(6)}"
    staged.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staged, filter="data")
    except (OSError, tarfile.TarError) as exc:
        shutil.rmtree(staged, ignore_errors=True)
        raise RestoreError("Не удалось распаковать media.") from exc
    if media_tree_sha256(staged) != expected_tree:
        shutil.rmtree(staged, ignore_errors=True)
        raise RestoreError("Хэш дерева media не совпал с manifest.")
    return staged


def restore_bundle(
    bundle: Path, identity: Path, public_key: Path, *,
    pg: dict, new_database: str, media_target: Path, private_target: Path,
    work_parent: Path, key_id: str = "production-1", pg_restore: str = "pg_restore",
    max_bytes: int = 20 * 1024**3,
) -> RestoreReport:
    started = time.monotonic()
    if not DB_NAME.fullmatch(new_database):
        raise RestoreError("Некорректное имя новой базы данных.")
    media_target, private_target = Path(media_target), Path(private_target)
    for target in (media_target, private_target):
        if target.exists():
            raise RestoreError("Целевой каталог media уже существует: перезапись запрещена.")
        target.parent.mkdir(parents=True, exist_ok=True)
    import psycopg
    from psycopg import sql

    common = {k: pg[k] for k in ("host", "port", "user", "password")}
    work = Path(tempfile.mkdtemp(prefix=".dr-restore-", dir=work_parent))
    os.chmod(work, 0o700)
    created_db = False
    staged: list[Path] = []
    placed: list[Path] = []
    checks: list[str] = []
    try:
        _extract_bundle(Path(bundle), Path(identity), work, max_bytes)
        checks.append("age расшифровка")
        manifest = _verify_contents(work, Path(public_key), key_id)
        checks.append("подпись manifest и SHA-256")
        public_stage = (
            _stage_media(work / "media.tar.gz", media_target.parent,
                         manifest["media_tree_sha256"])
            if (work / "media.tar.gz").is_file() else None
        )
        if public_stage:
            staged.append(public_stage)
        private_stage = _stage_media(
            work / "private_media.tar.gz", private_target.parent,
            manifest["private_media_tree_sha256"],
        )
        staged.append(private_stage)
        checks.append("хэши деревьев media и private_media")

        with psycopg.connect(dbname="postgres", autocommit=True, **common) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(new_database)))
            created_db = True
        env = {**os.environ, "PGPASSWORD": str(common["password"] or "")}
        subprocess.run(
            [pg_restore, "--no-owner", "--no-acl", "--exit-on-error",
             "-h", str(common["host"]), "-p", str(common["port"]), "-U", common["user"],
             "-d", new_database, str(work / "db.dump")],
            check=True, capture_output=True, env=env,
        )
        with psycopg.connect(dbname=new_database, **common) as restored:
            count = restored.execute("SELECT count(*) FROM django_migrations").fetchone()[0]
        if manifest.get("migration_state") and count != len(manifest["migration_state"]):
            raise RestoreError("Состав миграций восстановленной базы не совпал с manifest.")
        checks.append("pg_restore и сверка миграций")

        os.replace(public_stage, media_target) if public_stage else media_target.mkdir()
        placed.append(media_target)
        staged = [p for p in staged if p != public_stage]
        os.replace(private_stage, private_target)
        placed.append(private_target)
        staged = []
        checks.append("media размещены")
    except (RestoreError, OSError, subprocess.CalledProcessError, psycopg.Error, KeyError) as exc:
        for path in staged + placed:
            shutil.rmtree(path, ignore_errors=True)
        if created_db:
            with psycopg.connect(dbname="postgres", autocommit=True, **common) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(new_database)))
        raise RestoreError(
            f"Восстановление отменено, откат выполнен: {type(exc).__name__}"
        ) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return RestoreReport(
        new_database, media_target, private_target, time.monotonic() - started, checks,
    )
