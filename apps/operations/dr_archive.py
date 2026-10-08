"""Standard age-encrypted recovery bundle; only a recipient public key is needed."""

import hashlib
import os
import subprocess
import tarfile
from pathlib import Path

from .emergency_manifest import read_manifest, validate_manifest
from .manifest_signing import sign_manifest, verify_manifest


class ArchiveError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def encrypt_verified_run(
    run: Path, output: Path, recipient: str | list[str] | tuple[str, ...],
) -> tuple[int, str]:
    """Fail closed on missing/private data; never place the decrypt key on production."""
    run, output = Path(run), Path(output)
    manifest = read_manifest(run / "manifest.json")
    if manifest.get("private_media_filename") != "private_media.tar.gz":
        raise ArchiveError("Бэкап не содержит private_media.")
    if manifest.get("source_environment") == "production":
        verify_manifest(manifest)
    result = validate_manifest(run)
    if not result.ok:
        raise ArchiveError("Подписанный бэкап не прошёл проверку целостности.")
    names = {"manifest.json", *manifest.get("sha256", {})}
    if "private_media.tar.gz" not in names or manifest.get("db_file") not in names:
        raise ArchiveError("В манифесте отсутствуют обязательные компоненты.")
    if manifest.get("media_file") and manifest["media_file"] not in names:
        raise ArchiveError("Media отсутствует в перечне компонентов.")
    if any(Path(name).name != name for name in names):
        raise ArchiveError("Небезопасное имя компонента.")

    recipients = [recipient] if isinstance(recipient, str) else list(recipient)
    if not recipients or any(not r.startswith("age1") or not r.isalnum() for r in recipients):
        raise ArchiveError("Нужен публичный age recipient (age1...).")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(output.name + ".partial")
    if output.exists() or temporary_output.exists():
        raise ArchiveError("Выходной файл уже существует.")
    try:
        with subprocess.Popen(
            [
                "age", *[arg for r in recipients for arg in ("-r", r)],
                "-o", str(temporary_output),
            ],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
            if process.stdin is None:
                raise ArchiveError("Не удалось открыть age stdin.")
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                for name in sorted(names):
                    archive.add(run / name, arcname=name, recursive=False)
            process.stdin.close()
            if process.wait() != 0:
                raise ArchiveError("Шифрование age завершилось с ошибкой.")
        os.chmod(temporary_output, 0o600)
        temporary_output.replace(output)
        return output.stat().st_size, sha256_file(output)
    except (OSError, tarfile.TarError, ArchiveError) as exc:
        temporary_output.unlink(missing_ok=True)
        raise ArchiveError("Не удалось зашифровать архив age.") from exc


def signed_cipher_receipt(run_name: str, encrypted: Path) -> dict:
    """Bind the exact ciphertext to the existing production signer identity."""
    encrypted = Path(encrypted)
    receipt = {
        "version": 1,
        "run": run_name,
        "bytes": encrypted.stat().st_size,
        "sha256": sha256_file(encrypted),
    }
    sign_manifest(receipt)
    return receipt
