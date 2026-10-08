"""Cloud destinations (Yandex Object Storage, Google Drive) behind ``rclone``.

Honesty contract.  ``physical_bytes`` is the best measurement the provider API
offers for ONE dedicated namespace (live objects, every S3 version, Drive
trash).  It is not a provider quota: Google Drive has no per-folder limit and
Object Storage versions/multipart parts are only visible through listings.
Anything that cannot be measured raises ``BudgetError`` so the pipeline stops
instead of guessing.  See ``docs/operations/dr-1gb-implementation.md``.

This module never talks to a real account in tests: every rclone call goes
through the injectable ``runner``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .backup_budget import BYTE_LIMIT, BudgetError, Generation, reserve_capacity
from .dr_local import ENCRYPTED_NAME, RECEIPT_NAME, RUN_NAME, verify_receipt

Runner = Callable[..., bytes]

KINDS = ("s3", "drive")
INCOMPLETE_GRACE = timedelta(hours=6)


def rclone_runner(args: list[str], *, stdin: bytes | None = None, timeout: int = 1800) -> bytes:
    """Run rclone; never include arguments or stderr in errors (they may hold secrets)."""
    try:
        return subprocess.run(
            ["rclone", *args], input=stdin, check=True, capture_output=True, timeout=timeout,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BudgetError("Облачное хранилище недоступно или ответило ошибкой.") from exc


def stream_sha256(args: list[str], expected_bytes: int) -> str:
    """Hash ``rclone cat`` output, refusing more bytes than the signed size."""
    digest = hashlib.sha256()
    total = 0
    process = subprocess.Popen(
        ["rclone", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
            total += len(block)
            if total > expected_bytes:
                raise BudgetError("Облачный архив больше подписанного размера.")
            digest.update(block)
        if process.wait(timeout=60) != 0:
            raise BudgetError("Чтение облачного архива завершилось с ошибкой.")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BudgetError("Чтение облачного архива не удалось.") from exc
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if total != expected_bytes:
        raise BudgetError("Облачный архив короче подписанного размера.")
    return digest.hexdigest()


def validate_remote(kind: str, remote: str) -> str:
    """Require a dedicated sub-namespace: never a whole bucket or Drive root."""
    if kind not in KINDS:
        raise BudgetError("Неизвестный тип облака.")
    name, _, path = remote.partition(":")
    parts = [part for part in path.split("/") if part]
    if not name or ":" in path or ".." in parts or "\\" in remote:
        raise BudgetError("Некорректный rclone remote.")
    # s3: bucket + prefix; drive: at least one dedicated folder.
    if len(parts) < (2 if kind == "s3" else 1):
        raise BudgetError("Нужна выделенная папка/префикс DenisStock, а не корень хранилища.")
    return f"{name}:{'/'.join(parts)}"


def assert_isolated(remotes: list[str]) -> None:
    """Two destinations must never share (or nest) a namespace."""
    normalized = [remote.rstrip("/") + "/" for remote in remotes]
    for i, first in enumerate(normalized):
        for second in normalized[i + 1:]:
            if first.startswith(second) or second.startswith(first):
                raise BudgetError("Назначения пересекаются: ротация одного затронула бы другое.")


class RcloneDestination:
    """One dedicated cloud namespace holding ``<run>/bundle.tar.age`` + ``receipt.json``."""

    def __init__(
        self,
        kind: str,
        remote: str,
        public_key: Path,
        *,
        runner: Runner = rclone_runner,
        hasher: Callable[[list[str], int], str] = stream_sha256,
        limit: int = BYTE_LIMIT,
        key_id: str = "production-1",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.kind = kind
        self.remote = validate_remote(kind, remote)
        self.public_key = Path(public_key)
        self.runner = runner
        self.hasher = hasher
        self.limit = limit
        self.key_id = key_id
        self.now = now

    # -- flags -------------------------------------------------------------
    @property
    def _list_flags(self) -> list[str]:
        return ["--s3-versions"] if self.kind == "s3" else []

    def _json(self, *args: str):
        try:
            return json.loads(self.runner(list(args)) or b"null")
        except ValueError as exc:
            raise BudgetError("Облако вернуло нечитаемый ответ.") from exc

    # -- measurement -------------------------------------------------------
    def _size(self, *extra: str) -> int:
        data = self._json("size", "--json", *extra, self.remote)
        if not isinstance(data, dict) or not isinstance(data.get("bytes"), int):
            raise BudgetError("Облако не вернуло размер.")
        if data.get("sizeless", 0):
            raise BudgetError("Есть объекты без известного размера: измерение невозможно.")
        return data["bytes"]

    def physical_bytes(self) -> int:
        """Live + hidden versions (s3) or trashed files (drive) + failed uploads.

        Raises instead of under-reporting.
        """
        if self.kind == "s3":
            total = self._size("--s3-versions")
            self._assert_no_multipart()
            return total
        folder = self._size() + self._size("--drive-trashed-only")
        # The dedicated account is the real quota boundary: revisions and
        # anything outside the folder only show up in the account total.
        about = self._json("about", "--json", self.remote)
        used = about.get("used") if isinstance(about, dict) else None
        if not isinstance(used, int):
            raise BudgetError("Google Drive не вернул занятый объём аккаунта.")
        return max(folder, used)

    def _assert_no_multipart(self) -> None:
        bucket, _, prefix = self.remote.partition(":")[2].partition("/")
        root = f"{self.remote.partition(':')[0]}:{bucket}"
        uploads = self._json("backend", "list-multipart-uploads", root) or {}
        pending = [
            item for item in (uploads.get(bucket) or uploads.get("uploads") or [])
            if str(item.get("Key", "")).startswith(prefix)
        ] if isinstance(uploads, dict) else []
        if pending:
            raise BudgetError("Есть незавершённые multipart-загрузки: объём не измерить.")

    # -- listing and verification -------------------------------------------
    def _run_names(self) -> list[str]:
        out = self.runner(["lsf", "--dirs-only", "--max-depth", "1", self.remote]).decode()
        return sorted(n.rstrip("/") for n in out.splitlines() if RUN_NAME.fullmatch(n.rstrip("/")))

    def _files(self, name: str) -> list[dict]:
        data = self._json("lsjson", "--recursive", "--files-only", *self._list_flags,
                          f"{self.remote}/{name}")
        return data if isinstance(data, list) else []

    def generations(self) -> list[Generation]:
        return [Generation(name, self.is_verified(name)) for name in self._run_names()]

    def is_verified(self, name: str) -> bool:
        """Signed receipt + exact size + full read-back SHA-256 of the ciphertext."""
        try:
            files = {f["Path"]: f for f in self._files(name)}
            if set(files) != {ENCRYPTED_NAME, RECEIPT_NAME}:
                return False
            receipt = json.loads(self.runner(["cat", f"{self.remote}/{name}/{RECEIPT_NAME}"]))
            if (
                receipt.get("version") != 1
                or receipt.get("run") != name
                or not verify_receipt(receipt, self.public_key, self.key_id)
                or files[ENCRYPTED_NAME].get("Size") != receipt.get("bytes")
            ):
                return False
            digest = self.hasher(
                ["cat", f"{self.remote}/{name}/{ENCRYPTED_NAME}"], receipt["bytes"],
            )
            return digest == receipt.get("sha256")
        except (BudgetError, ValueError, TypeError, KeyError, AttributeError):
            return False

    # -- mutation -----------------------------------------------------------
    def delete_generation(self, name: str) -> None:
        """Remove ONE named generation: receipt first, so it is never half-trusted."""
        if not RUN_NAME.fullmatch(name):
            raise BudgetError("Некорректный идентификатор поколения.")
        base = f"{self.remote}/{name}"
        delete_flags = ["--s3-versions"] if self.kind == "s3" else ["--drive-use-trash=false"]
        files = self._files(name)
        ordered = sorted(files, key=lambda f: f["Path"] != RECEIPT_NAME)
        for item in ordered:
            if "/" in item["Path"] or ".." in item["Path"]:
                raise BudgetError("Небезопасный путь в поколении.")
            self.runner(["deletefile", *delete_flags, f"{base}/{item['Path']}"])
        self.runner(["rmdir", base])

    def discard_incomplete(self) -> list[str]:
        """Drop never-finished uploads (no receipt) older than the grace period.

        Without a receipt a generation was never verified recoverable, so
        removing it cannot reduce recovery evidence.
        """
        removed = []
        for name in self._run_names():
            files = self._files(name)
            if any(f["Path"] == RECEIPT_NAME for f in files):
                continue
            stamps = [f.get("ModTime") for f in files if f.get("ModTime")]
            if files and not stamps:
                continue
            newest = max(
                (datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps),
                default=None,
            )
            if newest is not None and self.now() - newest < INCOMPLETE_GRACE:
                continue
            self.delete_generation(name)
            removed.append(name)
        return removed

    def cleanup_multipart(self) -> None:
        """Abort stale incomplete multipart uploads (S3 only); parts are not evidence."""
        if self.kind == "s3":
            root = self.remote.partition("/")[0]
            self.runner(["backend", "cleanup", root, "-o", "max-age=1h"])

    def upload(self, name: str, bundle: Path, receipt_json: bytes) -> None:
        """Ciphertext first, receipt LAST: a receipt means the generation is complete."""
        if not RUN_NAME.fullmatch(name):
            raise BudgetError("Некорректный идентификатор поколения.")
        base = f"{self.remote}/{name}"
        self.runner(["copyto", "--immutable", str(bundle), f"{base}/{ENCRYPTED_NAME}"])
        self.runner(["rcat", f"{base}/{RECEIPT_NAME}"], stdin=receipt_json)


def publish_generation(
    destination: RcloneDestination, name: str, bundle: Path, receipt: dict,
) -> dict:
    """Rotate, upload, read back and re-measure ONE destination, fail-closed."""
    receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    incoming = Path(bundle).stat().st_size + len(receipt_bytes)
    if receipt.get("bytes") != Path(bundle).stat().st_size:
        raise BudgetError("Размер архива не совпал с receipt.")
    destination.cleanup_multipart()
    destination.discard_incomplete()
    if destination.is_verified(name):
        return {"run": name, "removed": [], "bytes": destination.physical_bytes(), "reused": True}
    removed = reserve_capacity(destination, incoming, limit=destination.limit)
    try:
        destination.upload(name, bundle, receipt_bytes)
        if not destination.is_verified(name):
            raise BudgetError("Загруженное поколение не прошло обратную проверку.")
        total = destination.physical_bytes()
        if total > destination.limit:
            raise BudgetError("После загрузки превышен лимит хранилища.")
    except BudgetError:
        # Only the unverified/over-limit generation we just wrote may be removed,
        # and only if another verified generation survives.
        others = [g for g in destination.generations() if g.verified and g.name != name]
        if others:
            destination.delete_generation(name)
        raise
    return {"run": name, "removed": removed, "bytes": total, "reused": False}
