"""Verified encrypted generations for a MacBook pull destination."""

import base64
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .backup_budget import (
    BYTE_LIMIT,
    BudgetError,
    Generation,
    parse_signed_time,
    prune_superseded,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$")
ENCRYPTED_NAME = "bundle.tar.age"
RECEIPT_NAME = "receipt.json"


def verify_receipt(
    receipt: dict, public_key_path: Path, expected_key_id: str = "production-1"
) -> bool:
    signature = receipt.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        return False
    if signature.get("key_id") != expected_key_id:
        return False
    try:
        key = serialization.load_pem_public_key(Path(public_key_path).read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            return False
        payload = {key: value for key, value in receipt.items() if key != "signature"}
        canonical = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ).encode("ascii")
        key.verify(base64.b64decode(signature["value"], validate=True), canonical)
        return True
    except (OSError, ValueError, TypeError, KeyError, InvalidSignature):
        return False


class LocalEncryptedStore:
    def __init__(
        self, root: Path, public_key: Path, *, limit: int = BYTE_LIMIT,
        key_id: str = "production-1",
    ):
        self.root = Path(root)
        self.public_key = Path(public_key)
        self.key_id = key_id
        self.limit = limit
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise BudgetError("Папка локальных бэкапов не может быть ссылкой.")

    def physical_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def generations(self) -> list[Generation]:
        result = []
        for path in self.root.iterdir():
            if path.is_dir() and RUN_NAME.fullmatch(path.name):
                result.append(Generation(path.name, self.is_verified(path.name)))
        return result

    def is_verified(self, name: str) -> bool:
        path = self._run_path(name)
        try:
            receipt = json.loads((path / RECEIPT_NAME).read_text())
            cipher = path / ENCRYPTED_NAME
            return (
                receipt.get("version") == 1
                and receipt.get("run") == name
                and verify_receipt(receipt, self.public_key, self.key_id)
                and cipher.is_file()
                and not cipher.is_symlink()
                and receipt.get("bytes") == cipher.stat().st_size
                and receipt.get("sha256") == sha256_file(cipher)
            )
        except (OSError, ValueError, TypeError):
            return False

    def signed_created_at(self, name: str):
        """Signed source-backup time of a VERIFIED generation, else None."""
        if not self.is_verified(name):
            return None
        try:
            receipt = json.loads((self._run_path(name) / RECEIPT_NAME).read_text())
        except (OSError, ValueError):
            return None
        return parse_signed_time(receipt.get("backup_created_at"))

    def delete_generation(self, name: str) -> None:
        path = self._run_path(name)
        if path.is_symlink() or not path.is_dir():
            raise BudgetError("Небезопасная папка поколения.")
        shutil.rmtree(path)

    def install(self, name: str, encrypted: Path, receipt: dict) -> list[str]:
        """Copy a downloaded ciphertext, verify, then atomically expose receipt."""
        target = self._run_path(name)
        if target.exists():
            raise BudgetError("Такое поколение уже существует.")
        encrypted = Path(encrypted)
        if not encrypted.is_file() or encrypted.is_symlink():
            raise BudgetError("Зашифрованный источник недоступен.")
        if not verify_receipt(receipt, self.public_key, self.key_id):
            raise BudgetError("Подпись receipt не прошла проверку.")
        expected_sha256 = receipt.get("sha256")
        if receipt.get("run") != name or sha256_file(encrypted) != expected_sha256:
            raise BudgetError("Контрольная сумма источника не совпала.")
        if receipt.get("bytes") != encrypted.stat().st_size:
            raise BudgetError("Размер источника не совпал с подписанным receipt.")
        receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        # The new copy must fit before any old verified generation is touched.
        if self.physical_bytes() + encrypted.stat().st_size + len(receipt_bytes) > self.limit:
            raise BudgetError("Новая копия не помещается без удаления проверенных поколений.")
        target.mkdir(mode=0o700)
        try:
            shutil.copyfile(encrypted, target / ENCRYPTED_NAME)
            if sha256_file(target / ENCRYPTED_NAME) != expected_sha256:
                raise BudgetError("Локальная копия повреждена при записи.")
            (target / RECEIPT_NAME).write_bytes(receipt_bytes)
            if not self.is_verified(name) or self.physical_bytes() > self.limit:
                raise BudgetError("Локальная копия не прошла проверку или превышен лимит.")
        except (OSError, BudgetError):
            shutil.rmtree(target)
            raise
        return self._prune_after(name)

    def install_staged(self, name: str, staged: Path, receipt: dict) -> list[str]:
        """Commit an already-budgeted download by rename, without a second copy."""
        target = self._run_path(name)
        staged = Path(staged)
        if (
            target.exists()
            or staged.parent != self.root
            or not staged.name.startswith(".incoming-")
        ):
            raise BudgetError("Некорректная временная копия.")
        if not staged.is_file() or staged.is_symlink():
            raise BudgetError("Временная копия отсутствует.")
        if (
            receipt.get("version") != 1
            or receipt.get("run") != name
            or not verify_receipt(receipt, self.public_key, self.key_id)
            or receipt.get("bytes") != staged.stat().st_size
            or receipt.get("sha256") != sha256_file(staged)
        ):
            raise BudgetError("Скачанный архив не прошёл проверку.")
        receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        # The staged ciphertext is already counted by physical_bytes().
        if self.physical_bytes() + len(receipt_bytes) > self.limit:
            raise BudgetError("Новая копия не помещается без удаления проверенных поколений.")
        target.mkdir(mode=0o700)
        try:
            os.replace(staged, target / ENCRYPTED_NAME)
            (target / RECEIPT_NAME).write_bytes(receipt_bytes)
            if not self.is_verified(name) or self.physical_bytes() > self.limit:
                raise BudgetError("Копия не прошла проверку или превышен лимит.")
        except (OSError, BudgetError):
            shutil.rmtree(target)
            raise
        return self._prune_after(name)

    def _prune_after(self, name: str) -> list[str]:
        """Older verified copies go only after the new one is verified in place."""
        if self.signed_created_at(name) is None:
            return []  # legacy receipt without a signed time: keep everything
        return prune_superseded(self, name)

    def _run_path(self, name: str) -> Path:
        if not RUN_NAME.fullmatch(name):
            raise BudgetError("Некорректный идентификатор поколения.")
        return self.root / name
