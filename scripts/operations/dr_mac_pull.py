#!/usr/bin/env python3
"""Pull the newest encrypted generation with a read-only rclone account."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.operations.backup_budget import BudgetError, parse_signed_time  # noqa: E402
from apps.operations.dr_local import (  # noqa: E402
    RUN_NAME,
    LocalEncryptedStore,
    verify_receipt,
)


def _rclone(*args: str) -> bytes:
    try:
        return subprocess.run(
            ["rclone", *args], check=True, capture_output=True, timeout=900,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BudgetError("Удалённое хранилище недоступно или ответило ошибкой.") from exc


def _download_bounded(source: str, target: Path, expected_bytes: int) -> None:
    """Never write more bytes than the capacity reserved before the download."""
    process = None
    try:
        process = subprocess.Popen(
            ["rclone", "cat", source, "--contimeout", "30s", "--timeout", "5m"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise BudgetError("Не удалось открыть поток скачивания.")
        remaining = expected_bytes
        with target.open("wb") as output:
            while remaining:
                block = process.stdout.read(min(1024 * 1024, remaining))
                if not block:
                    raise BudgetError("Удалённый архив оборвался при скачивании.")
                output.write(block)
                remaining -= len(block)
            if process.stdout.read(1):
                raise BudgetError("Удалённый архив больше зарезервированного объёма.")
        if process.wait(timeout=900) != 0:
            raise BudgetError("Удалённое скачивание завершилось с ошибкой.")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BudgetError("Скачивание не удалось.") from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()


def pull_newest(remote: str, root: Path, public_key: Path) -> str:
    if ":" not in remote or not remote.split(":", 1)[1].strip("/"):
        raise BudgetError("Нужна отдельная папка DenisStock в rclone remote.")
    store = LocalEncryptedStore(root, public_key)
    lock = store.root / ".pull.lock"
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        # This process owns the exclusive lock: stale unverified download files
        # cannot belong to another active invocation.
        for stale in store.root.glob(".incoming-*"):
            if stale.is_file() and not stale.is_symlink():
                stale.unlink()
        names = sorted(
            name.rstrip("/")
            for name in _rclone("lsf", remote, "--dirs-only", "--max-depth", "1")
            .decode("utf-8")
            .splitlines()
            if RUN_NAME.fullmatch(name.rstrip("/"))
        )
        if not names:
            raise BudgetError("В удалённом хранилище нет поколений.")
        local = newest_local_age(store.root, public_key)
        local_time = store.signed_created_at(local[0]) if local else None
        no_room = False
        for name in reversed(names):
            if store.is_verified(name):
                return name
            if (store.root / name).exists():
                # Preserve the local evidence and try an older remote run.
                continue
            try:
                receipt = json.loads(_rclone("cat", f"{remote}/{name}/receipt.json"))
                if (
                    receipt.get("version") != 1
                    or receipt.get("run") != name
                    or not verify_receipt(receipt, public_key)
                    or not isinstance(receipt.get("bytes"), int)
                    or receipt["bytes"] < 1
                    or not isinstance(receipt.get("sha256"), str)
                    or len(receipt["sha256"]) != 64
                ):
                    continue
                remote_time = parse_signed_time(receipt.get("backup_created_at"))
                if local_time is not None and (remote_time is None or remote_time <= local_time):
                    # Already holding a copy at least this fresh: never re-download
                    # older data the rotation removed on purpose.
                    return local[0]
                receipt_size = len((json.dumps(receipt, sort_keys=True) + "\n").encode())
                # A candidate must fit *without* deleting any known-good copy.
                if store.physical_bytes() + receipt["bytes"] + receipt_size > store.limit:
                    no_room = True
                    continue
                fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=store.root)
                os.close(fd)
                staged = Path(temporary)
                try:
                    _download_bounded(
                        f"{remote}/{name}/bundle.tar.age", staged, receipt["bytes"],
                    )
                    store.install_staged(name, staged, receipt)
                finally:
                    staged.unlink(missing_ok=True)
                return name
            except (BudgetError, ValueError, UnicodeDecodeError, OSError, TypeError):
                continue
        if no_room:
            raise BudgetError("Новая копия не помещается рядом с проверенной; старая сохранена.")
        raise BudgetError("Нет доступного проверенного поколения в этом хранилище.")


def newest_local_age(root: Path, public_key: Path) -> tuple[str, timedelta] | None:
    """Freshest locally verified generation by its signed source timestamp."""
    store = LocalEncryptedStore(root, public_key)
    freshest = None
    for generation in store.generations():
        if not generation.verified:
            continue
        try:
            receipt = json.loads((store.root / generation.name / "receipt.json").read_text())
            made = datetime.fromisoformat(receipt["backup_created_at"])
            if made.tzinfo is None:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if freshest is None or made > freshest[1]:
            freshest = (generation.name, made)
    if freshest is None:
        return None
    name, made = freshest
    return name, datetime.now(made.tzinfo) - made


def notify(message: str) -> None:
    """Best-effort macOS banner; the text never contains paths or secrets."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "DenisStock backup"'],
            check=False, timeout=10, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull the newest encrypted DenisStock backup")
    parser.add_argument(
        "--remote", required=True, action="append",
        help="rclone remote:isolated-folder (repeat for Yandex and Google)",
    )
    parser.add_argument("--root", required=True, type=Path, help="local encrypted backup directory")
    parser.add_argument(
        "--public-key", required=True, type=Path, help="pinned production signer key",
    )
    parser.add_argument("--max-age-hours", type=float, default=36)
    parser.add_argument("--notify", action="store_true", help="macOS notification on problems")
    args = parser.parse_args()
    pulled, failures = [], 0
    for remote in args.remote:
        try:
            pulled.append(pull_newest(remote.rstrip("/"), args.root, args.public_key))
        except BudgetError as exc:
            failures += 1
            print(f"DenisStock backup pull failed ({remote.split(':')[0]}): {exc}",
                  file=sys.stderr)
    status = 0
    if failures:
        status = 1 if failures == len(args.remote) else 3
    newest = newest_local_age(args.root, args.public_key)
    if (
        newest is None or newest[1] > timedelta(hours=args.max_age_hours)
        or newest[1] < -timedelta(minutes=5)
    ):
        print("DenisStock backup STALE: нет свежей проверенной локальной копии.", file=sys.stderr)
        status = status or 4
    if pulled:
        print(f"DenisStock encrypted backup verified: {max(pulled)}")
    if status and args.notify:
        notify("Резервная копия не обновлена. Проверьте журнал.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
