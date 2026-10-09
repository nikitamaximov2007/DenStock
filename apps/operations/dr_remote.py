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

import fcntl
import hashlib
import json
import subprocess
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .backup_budget import (
    BYTE_LIMIT,
    BudgetError,
    Generation,
    parse_signed_time,
    prune_superseded,
)
from .dr_local import ENCRYPTED_NAME, RECEIPT_NAME, RUN_NAME, verify_receipt

Runner = Callable[..., bytes]

KINDS = ("s3", "drive")


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
        if (
            not isinstance(data, dict) or not isinstance(data.get("bytes"), int)
            or data["bytes"] < 0
        ):
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
        if not isinstance(used, int) or used < 0:
            raise BudgetError("Google Drive не вернул занятый объём аккаунта.")
        # rclone maps used=usageInDrive and other=usage-usageInDrive (Gmail,
        # Photos).  Their sum is the account's whole quota usage: stricter.
        other = about.get("other", 0)
        if not isinstance(other, int) or other < 0:
            raise BudgetError("Google Drive вернул некорректный объём других сервисов.")
        return max(folder, used + other)

    def _assert_no_multipart(self) -> None:
        bucket, _, prefix = self.remote.partition(":")[2].partition("/")
        root = f"{self.remote.partition(':')[0]}:{bucket}"
        uploads = self._json("backend", "list-multipart-uploads", root)
        if not isinstance(uploads, dict):
            raise BudgetError("Список multipart-загрузок недоступен.")
        entries = uploads.get(bucket, uploads.get("uploads"))
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise BudgetError("Список multipart-загрузок неполон.")
        pending = [
            item for item in entries
            if str(item.get("Key", "")).startswith(prefix.rstrip("/") + "/")
        ]
        if pending:
            raise BudgetError("Есть незавершённые multipart-загрузки: объём не измерить.")

    # -- listing and verification -------------------------------------------
    def _run_names(self) -> list[str]:
        out = self.runner(["lsf", "--dirs-only", "--max-depth", "1", self.remote]).decode()
        names = [n.rstrip("/") for n in out.splitlines() if RUN_NAME.fullmatch(n.rstrip("/"))]
        if len(names) != len(set(names)):
            raise BudgetError("Облако вернуло неоднозначные имена поколений.")
        return sorted(names)

    def _files(self, name: str) -> list[dict]:
        data = self._json("lsjson", "--recursive", "--files-only", *self._list_flags,
                          f"{self.remote}/{name}")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise BudgetError("Облако вернуло неполный список файлов.")
        return data

    def generations(self) -> list[Generation]:
        return [Generation(name, self.is_verified(name)) for name in self._run_names()]

    def is_verified(self, name: str) -> bool:
        """Signed receipt + exact size + full read-back SHA-256 of the ciphertext."""
        try:
            listed = self._files(name)
            # Drive permits same-name files.  A dict would silently discard
            # one and rclone cat might subsequently read the other.
            if len(listed) != 2 or {f["Path"] for f in listed} != {
                ENCRYPTED_NAME, RECEIPT_NAME,
            }:
                return False
            files = {f["Path"]: f for f in listed}
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
        except (ValueError, TypeError, KeyError, AttributeError):
            return False

    # -- mutation -----------------------------------------------------------
    def _exact_pair(self, name: str) -> dict:
        """Exactly one ciphertext and one receipt; duplicates/extras are ambiguity."""
        listed = self._files(name)
        names = [f.get("Path") for f in listed]
        if sorted(names) != sorted([ENCRYPTED_NAME, RECEIPT_NAME]):
            raise BudgetError("Состав поколения неоднозначен; удаление запрещено.")
        ids = [f.get("ID") for f in listed if f.get("ID")]
        if len(ids) != len(set(ids)):
            raise BudgetError("Повторяющиеся идентификаторы объектов; удаление запрещено.")
        return {f["Path"]: f for f in listed}

    def signed_created_at(self, name: str):
        """Signed source-backup time of a VERIFIED generation, else None."""
        if not self.is_verified(name):
            return None
        try:
            receipt = json.loads(self.runner(["cat", f"{self.remote}/{name}/{RECEIPT_NAME}"]))
        except ValueError:
            return None
        if not verify_receipt(receipt, self.public_key, self.key_id):
            return None
        return parse_signed_time(receipt.get("backup_created_at"))

    def delete_generation(self, name: str) -> None:
        """Remove ONE superseded generation, receipt first so it is never half-trusted.

        Only ``prune_superseded`` calls this, after a newer generation was
        re-verified.  The exact pair is re-listed right before deletion.
        """
        if not RUN_NAME.fullmatch(name):
            raise BudgetError("Некорректный идентификатор поколения.")
        self._exact_pair(name)
        base = f"{self.remote}/{name}"
        flags = ["--drive-use-trash=false"] if self.kind == "drive" else []
        self.runner(["deletefile", *flags, f"{base}/{RECEIPT_NAME}"])
        self.runner(["deletefile", *flags, f"{base}/{ENCRYPTED_NAME}"])
        self.runner(["rmdir", base])

    def discard_incomplete(self) -> list[str]:
        """No automatic deletion from a possibly incomplete provider listing.

        A missing receipt in one listing is not proof that it never existed.
        Incomplete generations require an independently verified operator
        decision; leaving them in place is safer than erasing recovery data.
        """
        return []

    def cleanup_multipart(self) -> None:
        """Disabled: rclone's bucket-level cleanup may affect foreign prefixes."""
        return None

    def upload(self, name: str, bundle: Path, receipt_json: bytes) -> None:
        """Ciphertext first, receipt LAST: a receipt means the generation is complete."""
        if not RUN_NAME.fullmatch(name):
            raise BudgetError("Некорректный идентификатор поколения.")
        base = f"{self.remote}/{name}"
        self.runner(["copyto", "--immutable", str(bundle), f"{base}/{ENCRYPTED_NAME}"])
        self.runner(["rcat", f"{base}/{RECEIPT_NAME}"], stdin=receipt_json)


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def destination_lock(key: str):
    """Serialize publish/prune per destination within this host (threads and processes).

    It cannot stop a writer on another host or a person using the console:
    that limitation is documented, not hidden.
    """
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(digest, threading.Lock())
    directory = Path(tempfile.gettempdir()) / "denstock-dr-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    with thread_lock, (directory / f"{digest}.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def publish_generation(
    destination, name: str, bundle: Path, receipt: dict, *,
    allow_best_effort_budget: bool = False,
) -> dict:
    """Upload only if old and new fit together; prune superseded copies afterwards.

    Nothing is deleted before or during the upload.  After the new generation
    is verified by full read-back, verified generations with an OLDER signed
    creation time are removed so the next run has room again.
    """
    if not allow_best_effort_budget:
        raise BudgetError("Жёсткий лимит 1 ГБ не доказан; публикация остановлена.")
    key = getattr(destination, "remote", None) or f"{type(destination).__module__}." \
        f"{type(destination).__qualname__}"
    with destination_lock(key):
        return _publish_locked(destination, name, bundle, receipt)


def _publish_locked(destination, name: str, bundle: Path, receipt: dict) -> dict:
    receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    incoming = Path(bundle).stat().st_size + len(receipt_bytes)
    if receipt.get("bytes") != Path(bundle).stat().st_size:
        raise BudgetError("Размер архива не совпал с receipt.")
    reused = destination.is_verified(name)
    if reused and hasattr(destination, "runner"):
        # Same name must mean the same signed ciphertext, never "something verified".
        remote_receipt = json.loads(destination.runner(
            ["cat", f"{destination.remote}/{name}/{RECEIPT_NAME}"]))
        if (remote_receipt.get("sha256"), remote_receipt.get("bytes")) != (
            receipt.get("sha256"), receipt.get("bytes"),
        ):
            raise BudgetError("Имя поколения уже занято другим содержимым; публикация остановлена.")
    if not reused:
        measured = destination.physical_bytes()
        if measured < 0 or measured + incoming > destination.limit:
            raise BudgetError("Новая копия не помещается рядом с проверенной; алерт, без удаления.")
        destination.upload(name, bundle, receipt_bytes)
        if not destination.is_verified(name):
            raise BudgetError("Загруженное поколение не прошло обратную проверку.")
    # Prune BEFORE the final measurement: a run that finds an earlier prune
    # unfinished (reused generation) must be able to finish it.
    removed = (
        prune_superseded(destination, name)
        if hasattr(destination, "signed_created_at") else []
    )
    total = destination.physical_bytes()
    if total > destination.limit:
        raise BudgetError("Измеренный объём выше лимита; требуется вмешательство.")
    return {"run": name, "removed": removed, "reused": reused, "bytes": total}
