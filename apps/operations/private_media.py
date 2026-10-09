"""Backup and restore of PRIVATE_MEDIA_ROOT (customer attachments, AI screenshots,
catalog imports).

The archive is part of every production backup.  A backup must FAIL instead of
silently leaving private files out, so every unusual state is an explicit error:

* missing directory (volume not mounted)        -> ``PrivateMediaError``
* directory or file not readable                 -> ``PrivateMediaError``
* symlink or special file inside the tree        -> ``PrivateMediaError``
* leftovers of an interrupted restore            -> ``PrivateMediaError``
* tree changed while archiving (bots write here) -> retried, then ``PrivateMediaError``
* empty directory                                -> a valid archive with 0 files

Restore extracts into a staging directory INSIDE the target (same filesystem, so
the final step is a rename), verifies the content hash, then swaps the contents.
Any failure during the swap moves the previous contents back.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ARCHIVE_NAME = "private_media.tar.gz"
RESERVED_PREFIX = ".denstock-restore-"
CONSISTENCY_ATTEMPTS = 3


class PrivateMediaError(Exception):
    pass


@dataclass(frozen=True)
class Inventory:
    files: int
    bytes: int
    tree_sha256: str


def _digest(entries: list[tuple[PurePosixPath, str, int]]) -> Inventory:
    """Same algorithm as ``emergency_state.media_tree_sha256``: path + content hash."""
    digest = hashlib.sha256()
    total = 0
    for relative, file_hash, size in sorted(entries, key=lambda e: e[0].parts):
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(file_hash))
        total += size
    if not entries:
        digest.update(b"empty-media-tree\n")
    return Inventory(len(entries), total, digest.hexdigest())


def _hash_stream(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def inspect_tree(root, *, require_present: bool) -> Inventory | None:
    """Read every file once; refuse anything that would not round-trip safely."""
    root = Path(root)
    if root.is_symlink():
        raise PrivateMediaError("Каталог private_media является ссылкой: бэкап остановлен.")
    if not root.exists():
        if require_present:
            raise PrivateMediaError(
                f"Каталог private_media не найден ({root}): том не смонтирован."
            )
        return None
    if not root.is_dir():
        raise PrivateMediaError(f"private_media не является каталогом: {root}")
    if not os.access(root, os.R_OK | os.X_OK):
        raise PrivateMediaError("Нет прав на чтение каталога private_media.")

    def walk_error(exc: OSError):
        raise PrivateMediaError(
            f"Нет доступа к каталогу private_media: {_relative(root, exc.filename)}"
        ) from exc

    entries = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error, followlinks=False):
        here = Path(dirpath)
        if here == root:
            leftovers = [n for n in dirnames + filenames if n.startswith(RESERVED_PREFIX)]
            if leftovers:
                raise PrivateMediaError(
                    "В private_media остались каталоги прерванного восстановления: "
                    + ", ".join(sorted(leftovers))
                )
        for name in dirnames:
            path = here / name
            if path.is_symlink():
                raise PrivateMediaError(
                    f"Символическая ссылка в private_media: {_relative(root, path)}"
                )
            if not os.access(path, os.R_OK | os.X_OK):
                raise PrivateMediaError(
                    f"Нет доступа к каталогу private_media: {_relative(root, path)}"
                )
        for name in filenames:
            path = here / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise PrivateMediaError(
                    f"Символическая ссылка в private_media: {_relative(root, path)}"
                )
            if not stat.S_ISREG(mode):
                raise PrivateMediaError(
                    f"Специальный файл в private_media: {_relative(root, path)}"
                )
            if os.lstat(path).st_nlink > 1:
                # tar would store the second name as a link member, which the
                # safe restore filter refuses: never sign such an archive.
                raise PrivateMediaError(
                    f"Жёсткая ссылка в private_media: {_relative(root, path)}"
                )
            try:
                with path.open("rb") as handle:
                    file_hash = _hash_stream(handle)
                size = path.stat().st_size
            except OSError as exc:
                raise PrivateMediaError(
                    f"Файл private_media недоступен для чтения: {_relative(root, path)}"
                ) from exc
            entries.append((PurePosixPath(path.relative_to(root).as_posix()), file_hash, size))
    return _digest(entries)


def _relative(root: Path, path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except (TypeError, ValueError):
        return str(path)


def _member_path(name: str) -> PurePosixPath | None:
    """Normalized relative path of a member; None for the archive root entry."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise PrivateMediaError(f"Небезопасный путь в архиве private_media: {name}")
    parts = [part for part in path.parts if part != "."]
    if not parts:
        return None
    if parts[0].startswith(RESERVED_PREFIX):
        raise PrivateMediaError(f"Зарезервированный путь в архиве private_media: {name}")
    return PurePosixPath(*parts)


def archive_inventory(archive) -> Inventory:
    """Verify every member (regular file or directory, safe path, unique) and hash it."""
    seen = set()
    entries = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                relative = _member_path(member.name)
                if not (member.isreg() or member.isdir()):
                    raise PrivateMediaError(
                        f"Недопустимый тип элемента в архиве private_media: {member.name}"
                    )
                if relative is None:
                    continue
                if relative in seen:
                    raise PrivateMediaError(
                        f"Повтор элемента в архиве private_media: {member.name}"
                    )
                seen.add(relative)
                if member.isreg():
                    stream = tar.extractfile(member)
                    entries.append((relative, _hash_stream(stream), member.size))
            # tarfile stops at the end-of-archive marker; reading the gzip
            # stream to its end makes gzip verify the trailer CRC and length.
            while tar.fileobj.read(1024 * 1024):
                pass
    except PrivateMediaError:
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError) as exc:
        raise PrivateMediaError(f"Архив private_media повреждён: {exc}") from exc
    return _digest(entries)


def _refuse_unsafe_members(tarinfo: tarfile.TarInfo):
    # A symlink or device created between inspection and archiving must not
    # slip into the archive.
    if not (tarinfo.isreg() or tarinfo.isdir()):
        raise PrivateMediaError(f"Недопустимый элемент private_media: {tarinfo.name}")
    return tarinfo


def create_archive(root, dest_dir) -> tuple[Path, Inventory]:
    """Archive the tree; the archive's own content must equal the source before and after."""
    root, dest_dir = Path(root), Path(dest_dir)
    final = dest_dir / ARCHIVE_NAME
    partial = dest_dir / (ARCHIVE_NAME + ".partial")
    for _attempt in range(CONSISTENCY_ATTEMPTS):
        before = inspect_tree(root, require_present=True)
        try:
            with tarfile.open(partial, "w:gz") as tar:
                tar.add(str(root), arcname=".", filter=_refuse_unsafe_members)
            packed = archive_inventory(partial)
        except FileNotFoundError:
            # A bot removed a delivered attachment mid-way: take a fresh snapshot.
            partial.unlink(missing_ok=True)
            continue
        except PrivateMediaError:
            partial.unlink(missing_ok=True)
            raise
        except (OSError, tarfile.TarError) as exc:
            partial.unlink(missing_ok=True)
            raise PrivateMediaError(f"Не удалось заархивировать private_media: {exc}") from exc
        after = inspect_tree(root, require_present=True)
        if before == after == packed:
            os.replace(partial, final)
            return final, packed
        partial.unlink(missing_ok=True)  # a bot added/removed a file: take a fresh snapshot
    raise PrivateMediaError(
        "private_media изменялся во время копирования; согласованный архив не получен."
    )


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def restore_archive(archive, root, *, expected: Inventory) -> Inventory:
    """Replace the contents of ``root`` with the archive, all or nothing.

    ``expected`` is the inventory recorded in the (signature-checked) manifest;
    there is deliberately no way to restore an archive without one.
    """
    root = Path(root)
    packed = archive_inventory(archive)
    if packed != expected:
        raise PrivateMediaError("Архив private_media не совпадает с manifest.")
    if root.is_symlink():
        raise PrivateMediaError("Каталог private_media является ссылкой.")
    root.mkdir(parents=True, exist_ok=True)
    leftovers = sorted(p.name for p in root.iterdir() if p.name.startswith(RESERVED_PREFIX))
    if leftovers:
        raise PrivateMediaError(
            "Предыдущее восстановление private_media было прервано; разберите вручную: "
            + ", ".join(leftovers)
        )
    token = uuid.uuid4().hex
    staging = root / f"{RESERVED_PREFIX}{token}-new"
    previous = root / f"{RESERVED_PREFIX}{token}-old"
    staging.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        if inspect_tree(staging, require_present=True) != packed:
            raise PrivateMediaError("Распакованные private_media не совпадают с архивом.")
    except (OSError, tarfile.TarError, PrivateMediaError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, PrivateMediaError):
            raise
        raise PrivateMediaError(f"Не удалось распаковать private_media: {exc}") from exc

    previous.mkdir(mode=0o700)
    moved, placed = [], []
    try:
        for entry in sorted(root.iterdir()):
            if entry.name.startswith(RESERVED_PREFIX):
                continue
            os.replace(entry, previous / entry.name)
            moved.append(entry.name)
        for entry in sorted(staging.iterdir()):
            os.replace(entry, root / entry.name)
            placed.append(entry.name)
        staging.rmdir()
        if _live_inventory(root) != packed:
            raise PrivateMediaError("private_media после восстановления не совпадают с архивом.")
    except (OSError, PrivateMediaError) as exc:
        for name in placed:
            _remove(root / name)
        for name in moved:
            os.replace(previous / name, root / name)
        shutil.rmtree(staging, ignore_errors=True)
        previous.rmdir()
        raise PrivateMediaError(
            f"Восстановление private_media отменено, прежние файлы возвращены: {exc}"
        ) from exc
    shutil.rmtree(previous)
    return packed


def _live_inventory(root: Path) -> Inventory:
    """Inventory of ``root`` ignoring this restore's own (still present) old copy."""
    entries = []
    for path in root.rglob("*"):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if relative.parts[0].startswith(RESERVED_PREFIX) or not path.is_file():
            continue
        with path.open("rb") as handle:
            entries.append((relative, _hash_stream(handle), path.stat().st_size))
    return _digest(entries)
