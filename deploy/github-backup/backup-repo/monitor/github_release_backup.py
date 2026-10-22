#!/usr/bin/env python3
"""Daily encrypted DenisStock backups in a PRIVATE GitHub repository's Releases.

    github_release_backup.py push    # VPS, daily, after backup_all (write token)
    github_release_backup.py check   # NOT on the VPS: FRESH / STALE / CORRUPT (read token)
    github_release_backup.py fetch   # recovery: download + verify one generation

One generation = one release ``denstock-backup-<RUN>-<manifest sha12>`` with two
assets: ``<RUN>.signed-backup.tar.age`` (the whole signed run: db.dump,
media.tar.gz, private_media.tar.gz, manifest.json, encrypted on this host to
the owner's age RECIPIENT) and ``<RUN>.backup.json`` (sizes and SHA-256, no
secrets). A release is created as a DRAFT, its assets are uploaded and read
back, and only then is it published: a draft never counts as a recovery point.

Storage: every asset byte of every release in the repository is counted (also
releases this job does not recognize). Before an upload the job prunes the
oldest of ITS OWN generations down to a watermark (default 850 MiB), always
keeping at least two valid published generations, and refuses to upload when
the result would exceed the hard budget (default 1 GiB). Unrecognized
releases and generations whose title contains ``[keep]`` are never deleted.

The client uses the Python standard library for GitHub HTTP calls and
``cryptography`` to verify the backup manifest signature. The token is read
from a file and never printed; the age identity (private key) must never be on the VPS.
Configuration: ``KEY=VALUE`` lines in ``$GHB_CONFIG`` (default
/etc/denstock/github-backup.env) or the environment (environment wins).
"""

from __future__ import annotations

# The monitor also runs on macOS /usr/bin/python3 (3.9): no datetime.UTC, and
# socket.timeout is not yet TimeoutError there.
# ruff: noqa: UP017, UP041
import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "denstock-github-backup/1"
MARKER = "<!-- denstock-backup-generation v1 -->"
TAG_RE = re.compile(r"^denstock-backup-(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-([0-9a-f]{12})$")
RUN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{58}$")
MIB = 1024 * 1024
CHUNK = MIB


class BackupError(Exception):
    """A failure with an operator-readable Russian message (no secrets)."""


class Stop(BaseException):
    """SIGTERM/SIGINT: unwinds through ``finally`` so status and temp files are handled."""


# --- configuration ----------------------------------------------------------------


def load_config() -> dict:
    conf: dict[str, str] = {}
    path = Path(os.environ.get("GHB_CONFIG", "/etc/denstock/github-backup.env"))
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                conf[key.strip()] = value.strip()
    for key, value in os.environ.items():
        if key.startswith("GHB_") or key == "BACKUP_ROOT":
            conf[key] = value
    return conf


def setting(conf, key, default=None):
    value = conf.get(key, default)
    if value in (None, ""):
        raise BackupError(f"не задан параметр {key}")
    return value


def read_token(conf) -> str:
    path = Path(setting(conf, "GHB_TOKEN_FILE"))
    try:
        token = path.read_text().strip()
    except OSError:
        raise BackupError("файл токена GitHub не читается") from None
    if not token or any(ch.isspace() for ch in token):
        raise BackupError("файл токена GitHub пуст или поврежден")
    return token


# --- GitHub REST client -----------------------------------------------------------


class GitHub:
    def __init__(self, conf, token):
        self.repo = setting(conf, "GHB_REPO")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise BackupError("GHB_REPO должен иметь вид owner/name")
        self.api = conf.get("GHB_API_URL", "https://api.github.com").rstrip("/")
        self.uploads = conf.get("GHB_UPLOAD_URL", "https://uploads.github.com").rstrip("/")
        self.timeout = float(conf.get("GHB_TIMEOUT", "120"))
        self.retries = int(conf.get("GHB_RETRIES", "3"))
        self.retry_delay = float(conf.get("GHB_RETRY_DELAY", "5"))
        self._token = token

    def _headers(self, extra=None, auth=True):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "denstock-github-backup"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        headers.update(extra or {})
        return headers

    @staticmethod
    def _explain(status: int, what: str) -> BackupError:
        if status == 401:
            return BackupError(
                f"GitHub отклонил токен (401): токен недействителен или просрочен ({what})")
        if status == 403:
            return BackupError(
                f"GitHub запретил операцию (403): нет прав или превышен лимит ({what})")
        if status == 404:
            return BackupError(
                f"GitHub: не найдено (404): нет доступа к репозиторию или объекту ({what})")
        if status >= 500:
            return BackupError(f"GitHub недоступен ({status}) ({what})")
        return BackupError(f"GitHub ответил {status} ({what})")

    def _open(self, request):
        opener = urllib.request.build_opener(_NoRedirect)
        return opener.open(request, timeout=self.timeout)

    def call(self, method, path, body=None, *, what, ok=(200, 201, 204), retry=None):
        url = path if path.startswith("http") else f"{self.api}{path}"
        data = json.dumps(body).encode() if body is not None else None
        retry = (method == "GET") if retry is None else retry
        attempts = self.retries if retry else 1
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=data, method=method, headers=self._headers(
                {"Content-Type": "application/json"} if data is not None else None))
            try:
                with self._open(request) as response:
                    return _json(response.read(), what)
            except urllib.error.HTTPError as exc:
                if exc.code in ok:
                    return _json(exc.read(), what)
                if exc.code >= 500 and attempt < attempts:
                    time.sleep(self.retry_delay * attempt)
                    continue
                wait = _rate_limit_wait(exc)
                if wait is not None:
                    # Primary or secondary rate limit. A read waits once if GitHub
                    # asks for at most a minute; a write never repeats by itself.
                    if retry and attempt < attempts and wait <= 60:
                        time.sleep(wait)
                        continue
                    raise BackupError(f"лимит запросов GitHub исчерпан ({what})") from None
                raise self._explain(exc.code, what) from None
            except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError,
                    http.client.HTTPException) as exc:
                if attempt < attempts:
                    time.sleep(self.retry_delay * attempt)
                    continue
                reason = getattr(exc, "reason", exc)
                raise BackupError(f"GitHub недоступен: {type(reason).__name__} ({what})") from None
        raise BackupError(f"GitHub недоступен ({what})")

    def repository(self):
        return self.call("GET", f"/repos/{self.repo}", what="репозиторий")

    def _pages(self, path, what):
        result, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            batch = self.call("GET", f"{path}{sep}per_page=100&page={page}", what=what)
            if not isinstance(batch, list) or not all(isinstance(x, dict) for x in batch):
                raise BackupError(f"GitHub вернул некорректный ответ ({what})")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1
            if page > 1000:
                raise BackupError(f"слишком длинный список ({what})")

    def releases(self):
        """Every release (drafts too, with a write token) with its COMPLETE asset
        list. Assets come from the paginated assets endpoint, not from the copy
        embedded in the release listing, so the byte count never misses one."""
        releases = self._pages(f"/repos/{self.repo}/releases", "список выпусков")
        for release in releases:
            if not isinstance(release.get("id"), int):
                raise BackupError("GitHub вернул выпуск без идентификатора")
            release["assets"] = self._pages(
                f"/repos/{self.repo}/releases/{release['id']}/assets", "список файлов выпуска")
        return releases

    def create_draft(self, tag, body):
        return self.call("POST", f"/repos/{self.repo}/releases", {
            "tag_name": tag, "name": tag, "body": body, "draft": True}, what="создание черновика")

    def publish(self, release_id):
        return self.call("PATCH", f"/repos/{self.repo}/releases/{release_id}", {"draft": False},
                         what="публикация выпуска")

    def delete_release(self, release_id):
        self.call("DELETE", f"/repos/{self.repo}/releases/{release_id}", what="удаление выпуска",
                  retry=False)

    def upload(self, release_id, name, path: Path):
        url = (f"{self.uploads}/repos/{self.repo}/releases/{release_id}/assets?"
               + urllib.parse.urlencode({"name": name}))
        size = path.stat().st_size
        with path.open("rb") as fh:
            request = urllib.request.Request(url, data=fh, method="POST", headers=self._headers({
                "Content-Type": "application/octet-stream", "Content-Length": str(size)}))
            try:
                with self._open(request) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                raise self._explain(exc.code, f"выгрузка {name}") from None
            except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError,
                    http.client.HTTPException, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise BackupError(f"выгрузка {name} прервана: {type(reason).__name__}") from None

    def download(self, asset_id, sink, *, what):
        """Stream an asset into ``sink(chunk)``; returns the byte count.

        GitHub answers 302 to a signed storage URL. The redirect is followed by
        hand WITHOUT the Authorization header: the token must never reach the
        storage host (and the storage host rejects a second auth mechanism).
        """
        url = f"{self.api}/repos/{self.repo}/releases/assets/{asset_id}"
        request = urllib.request.Request(url, headers=self._headers(
            {"Accept": "application/octet-stream"}))
        try:
            try:
                response = self._open(request)
            except urllib.error.HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    raise self._explain(exc.code, what) from None
                location = exc.headers.get("Location")
                if not location or not location.startswith(("https://", "http://127.0.0.1",
                                                             "http://localhost")):
                    raise BackupError(
                        f"GitHub вернул некорректное перенаправление ({what})") from None
                response = self._open(urllib.request.Request(location, headers={
                    "User-Agent": "denstock-github-backup"}))
            total = 0
            with response:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        return total
                    total += len(chunk)
                    sink(chunk)
        except urllib.error.HTTPError as exc:
            raise self._explain(exc.code, what) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError,
                http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            raise BackupError(f"чтение {what} прервано: {type(reason).__name__}") from None


def _json(raw, what):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise BackupError(f"GitHub вернул не JSON ({what})") from None


def _rate_limit_wait(exc):
    """Seconds to wait for a rate-limit answer, or None if it is not one."""
    headers = exc.headers or {}
    limited = exc.code == 429 or (exc.code == 403 and (
        headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0"))
    if not limited:
        return None
    try:
        if headers.get("Retry-After"):
            return float(headers["Retry-After"])
        return max(0.0, float(headers.get("X-RateLimit-Reset", "0")) - time.time())
    except ValueError:
        return 3600.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ARG002
        return None


# --- generations ------------------------------------------------------------------


def asset_names(run):
    return f"{run}.signed-backup.tar.age", f"{run}.backup.json"


def release_bytes(release):
    total = 0
    for asset in release.get("assets") or []:
        size = asset.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            # An unknown size makes the budget unknowable: fail closed.
            raise BackupError("GitHub вернул файл выпуска без корректного размера")
        total += size
    return total


def classify(gh: GitHub, release, *, read_sidecar=True):
    """-> dict(kind=unrecognized|incomplete|valid|invalid, run, keep, bytes, reason, ...)."""
    tag = release.get("tag_name") or ""
    match = TAG_RE.match(tag)
    info = {"id": release.get("id"), "tag": tag, "bytes": release_bytes(release),
            "keep": "[keep]" in (release.get("name") or "").lower(), "run": None}
    if not match or MARKER not in (release.get("body") or ""):
        return {**info, "kind": "unrecognized"}
    info["run"] = match.group(1)
    if release.get("draft"):
        return {**info, "kind": "incomplete", "reason": "черновик: выгрузка не завершилась"}
    cipher_name, sidecar_name = asset_names(info["run"])
    assets = {a["name"]: a for a in release.get("assets") or []}
    if set(assets) != {cipher_name, sidecar_name}:
        return {**info, "kind": "invalid", "reason": "набор файлов выпуска неполный или лишний"}
    if any(a.get("state") != "uploaded" for a in assets.values()):
        return {**info, "kind": "invalid", "reason": "файл выпуска выгружен не полностью"}
    info["cipher_asset"], info["sidecar_asset"] = assets[cipher_name], assets[sidecar_name]
    if not read_sidecar:
        return {**info, "kind": "valid"}
    raw = bytearray()
    if assets[sidecar_name].get("size", 0) > 64 * 1024:
        return {**info, "kind": "invalid", "reason": "описание копии подозрительно большое"}
    try:
        gh.download(assets[sidecar_name]["id"], raw.extend, what=sidecar_name)
        sidecar = json.loads(bytes(raw))
    except ValueError:
        return {**info, "kind": "invalid", "reason": "описание копии не читается"}
    if not isinstance(sidecar, dict):
        return {**info, "kind": "invalid", "reason": "описание копии не читается"}
    info["sidecar"] = sidecar
    if (sidecar.get("format") != FORMAT or sidecar.get("tag") != tag
            or sidecar.get("run") != info["run"]
            or sidecar.get("ciphertext_name") != cipher_name
            or not re.fullmatch(r"[0-9a-f]{64}", str(sidecar.get("ciphertext_sha256")))
            or sidecar.get("ciphertext_bytes") != assets[cipher_name].get("size")
            or _parse_time(str(sidecar.get("created_at") or "")) is None):
        return {**info, "kind": "invalid", "reason": "размер или описание не совпадают"}
    return {**info, "kind": "valid", "created_at": sidecar.get("created_at")}


def inventory(gh: GitHub):
    gens = [classify(gh, r) for r in gh.releases()]
    gens.sort(key=lambda g: (g["run"] or "", g["tag"]))
    return gens


def total_bytes(gens):
    return sum(g["bytes"] for g in gens)


def plan_prune(gens, need, watermark, budget, min_keep):
    """Oldest-first deletions so that used + need fits. Never touches unrecognized
    or [keep] releases and never leaves fewer than ``min_keep`` valid generations.
    Returns (to_delete, used_after)."""
    used = total_bytes(gens)
    valid = sum(1 for g in gens if g["kind"] == "valid")
    delete = []
    # Invalid generations are never recovery points: they go first, oldest first.
    candidates = [g for g in gens if g["kind"] == "invalid" and not g["keep"]]
    candidates += [g for g in gens if g["kind"] == "valid" and not g["keep"]]
    for gen in candidates:
        if used + need <= watermark:
            break
        if gen["kind"] == "valid":
            if valid <= min_keep:
                break
            valid -= 1
        delete.append(gen)
        used -= gen["bytes"]
    return delete, used


def plan_with_proven_survivors(gh, gens, need, watermark, budget, min_keep):
    """Before a VALID generation is deleted, the valid generations that will remain
    as the last recovery points are read back in full and their SHA-256 compared
    with their description. A silently corrupted one is demoted to invalid and the
    plan is made again, so rotation never leaves only damaged copies behind."""
    proven = set()
    while True:
        delete, after = plan_prune(gens, need, watermark, budget, min_keep)
        if not any(g["kind"] == "valid" for g in delete):
            return delete, after
        survivors = [g for g in gens if g["kind"] == "valid" and g not in delete][-min_keep:]
        demoted = False
        for gen in survivors:
            if gen["tag"] in proven:
                continue
            sidecar = gen["sidecar"]
            if read_back(gh, gen["cipher_asset"]["id"], what=gen["tag"]) != (
                    sidecar["ciphertext_bytes"], sidecar["ciphertext_sha256"]):
                log(f"копия {gen['tag']} повреждена (SHA-256): больше не считается целой")
                gen.update(kind="invalid", reason="SHA-256 не совпадает")
                demoted = True
            else:
                proven.add(gen["tag"])
        if not demoted:
            return delete, after


# --- status and notification ------------------------------------------------------


class Status:
    def __init__(self, conf):
        root = conf.get("BACKUP_ROOT", "/opt/denstock/backups")
        self.path = Path(conf.get("GHB_STATUS_FILE", f"{root}/github_backup_status.json"))
        self.notify_script = conf.get("GHB_NOTIFY_SCRIPT", "")
        self.data = {"status": "running", "message": "Выполняется", "run": "", "tag": ""}

    def write(self, state, message, **extra):
        self.data.update(extra, status=state, message=message,
                         checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1) + "\n")
        tmp.replace(self.path)
        if state == "failed" and self.notify_script:
            try:  # the message goes on stdin, never into argv
                subprocess.run([self.notify_script, "github-backup", state],
                               input=message.encode(), timeout=60, check=False)
            except (OSError, subprocess.SubprocessError):
                pass


def log(message):
    print(f"[github-backup] {message}", file=sys.stderr, flush=True)


# --- push -------------------------------------------------------------------------


def pick_run(root: Path, requested: str | None):
    if requested:
        if not RUN_RE.match(requested):
            raise BackupError("GHB_RUN имеет неверный формат")
        return requested
    for run in sorted((p for p in root.iterdir() if p.is_dir() and RUN_RE.match(p.name)),
                      reverse=True):
        try:
            manifest = json.loads((run / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        if manifest.get("verification_status") == "verified":
            return run.name
    raise BackupError("нет локальной копии со статусом verified")


def verify_signature(manifest: dict, pubkey_path: Path) -> None:
    """Ed25519 over the canonical manifest without ``signature``: the same bytes
    that backup_all signs and that offline_restore.sh verifies. A copy whose
    signature does not verify against the pinned key never leaves the host."""
    import base64

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise BackupError("нет python3-cryptography: подпись проверить нельзя") from None
    try:
        key = serialization.load_pem_public_key(pubkey_path.read_bytes())
    except (OSError, ValueError):
        raise BackupError("публичный ключ подписи не читается") from None
    signature = manifest.get("signature") or {}
    if signature.get("algorithm") != "ed25519" or not signature.get("value"):
        raise BackupError("manifest не подписан Ed25519")
    payload = json.dumps({k: v for k, v in manifest.items() if k != "signature"},
                         ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    try:
        key.verify(base64.b64decode(signature["value"]), payload)
    except (InvalidSignature, ValueError, TypeError):
        raise BackupError("подпись manifest не прошла проверку: копию не выгружаю") from None


def check_local_run(run_dir: Path, pubkey_path: Path):
    raw = (run_dir / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest.get("verification_status") != "verified":
        raise BackupError(f"копия {run_dir.name} не проверена (verification_status)")
    if manifest.get("private_media_status") != "included":
        raise BackupError(f"в копии {run_dir.name} нет private_media: такую копию не выгружаю")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("app_commit") or "")):
        raise BackupError("manifest не содержит полный Git SHA исходников")
    for name in ("db.dump", "manifest.json"):
        if not (run_dir / name).is_file():
            raise BackupError(f"в копии {run_dir.name} нет {name}")
    hashes = manifest.get("sha256") or {}
    required = ["db.dump", manifest.get("private_media_filename") or "private_media.tar.gz"]
    if manifest.get("media_filename"):
        required.append(manifest["media_filename"])  # public media, when the site has any
    for name in required:
        if name not in hashes:
            raise BackupError(f"manifest не содержит хеш {name}")
    for name, digest in hashes.items():
        if sha256_file(run_dir / name) != digest:
            raise BackupError(f"локальная копия повреждена: хеш {name} не совпадает")
    verify_signature(manifest, pubkey_path)
    return manifest, hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_recipient(conf) -> str:
    text = Path(setting(conf, "GHB_RECIPIENT_FILE")).read_text()
    if "AGE-SECRET-KEY" in text.upper():
        raise BackupError("в файле получателя лежит ЗАКРЫТЫЙ ключ age: ему не место на сервере")
    recipient = "".join(text.split())
    if not RECIPIENT_RE.match(recipient):
        raise BackupError("получатель age некорректен")
    return recipient


def encrypt_run(root: Path, run: str, recipient: str, out: Path):
    """tar | age straight into a ciphertext file: plaintext never touches the disk."""
    partial = out.with_suffix(out.suffix + ".partial")
    with partial.open("xb") as sink:
        tar = subprocess.Popen(["tar", "-C", str(root), "-cf", "-", run], stdout=subprocess.PIPE)
        age = subprocess.Popen(["age", "-r", recipient], stdin=tar.stdout, stdout=sink)
        tar.stdout.close()
        age_code, tar_code = age.wait(), tar.wait()
    if tar_code or age_code:
        partial.unlink(missing_ok=True)
        raise BackupError("шифрование копии не удалось (tar или age)")
    partial.replace(out)


def read_back(gh, asset_id, *, what):
    digest = hashlib.sha256()
    size = gh.download(asset_id, digest.update, what=what)
    return size, digest.hexdigest()


def push(conf) -> int:
    status = Status(conf)
    lock_path = Path(conf.get("GHB_LOCK_FILE", "/run/lock/denstock-github-backup.lock"))
    lock = lock_path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        log("другой запуск еще выполняется: этот завершается без изменений")
        return 75
    work = None
    cipher = None
    try:
        status.write("running", "Шифрование и выгрузка на GitHub выполняются")
        root = Path(setting(conf, "BACKUP_ROOT"))
        work = Path(conf.get("GHB_WORK_DIR", str(root / ".github-upload")))
        budget = int(conf.get("GHB_BUDGET_BYTES", str(1024 * MIB)))
        watermark = int(conf.get("GHB_WATERMARK_BYTES", str(850 * MIB)))
        min_keep = int(conf.get("GHB_MIN_KEEP", "2"))
        if (min_keep < 2 or not 0 < watermark <= budget
                or budget > 1024 * MIB or watermark > 850 * MIB):
            raise BackupError("неверные GHB_MIN_KEEP / GHB_WATERMARK_BYTES / GHB_BUDGET_BYTES")
        recipient = read_recipient(conf)
        gh = GitHub(conf, read_token(conf))
        repo = gh.repository()
        if not repo.get("private"):
            raise BackupError("репозиторий для копий не приватный: выгрузка запрещена")
        forbidden = {r.strip().lower() for r in conf.get("GHB_FORBIDDEN_REPOS", "").split(",")}
        if gh.repo.lower() in forbidden or (repo.get("full_name") or "").lower() in forbidden:
            raise BackupError("это репозиторий исходного кода, а не репозиторий копий")

        run = pick_run(root, conf.get("GHB_RUN"))
        status.data.update(run=run)
        pubkey = Path(conf.get("GHB_MANIFEST_PUBKEY",
                               "/etc/denstock/manifest-signing/production-ed25519.pub"))
        manifest, manifest_sha = check_local_run(root / run, pubkey)
        tag = f"denstock-backup-{run}-{manifest_sha[:12]}"
        status.data.update(tag=tag)
        cipher_name, sidecar_name = asset_names(run)

        gens = inventory(gh)
        same = [g for g in gens if g["tag"] == tag]
        if same and same[0]["kind"] == "valid":
            status.write("ok", f"Копия {run} уже выгружена и проверена ранее",
                         **usage(gens, budget, watermark))
            return 0
        if same and same[0]["kind"] in ("invalid", "unrecognized"):
            raise BackupError(f"выпуск {tag} уже существует и не подтвержден: не перезаписываю")
        newest_valid = max((g["run"] for g in gens if g["kind"] == "valid"), default="")
        if newest_valid > run:
            status.write("ok", f"На GitHub уже есть более новая копия {newest_valid}",
                         **usage(gens, budget, watermark))
            return 0

        # Our own unfinished drafts are never recovery points: remove them first.
        for gen in [g for g in gens if g["kind"] == "incomplete"]:
            log(f"удаляю незавершенный черновик {gen['tag']}")
            gh.delete_release(gen["id"])
        gens = [g for g in gens if g["kind"] != "incomplete"]

        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        for stale in work.glob("*.partial"):
            stale.unlink()
        cipher = work / cipher_name
        cipher.unlink(missing_ok=True)
        encrypt_run(root, run, recipient, cipher)
        cipher_bytes, cipher_sha = cipher.stat().st_size, sha256_file(cipher)
        sidecar = json.dumps({
            "format": FORMAT, "tag": tag, "run": run, "created_at": manifest.get("created_at"),
            "app_commit": manifest.get("app_commit"), "manifest_sha256": manifest_sha,
            "ciphertext_name": cipher_name, "ciphertext_bytes": cipher_bytes,
            "ciphertext_sha256": cipher_sha, "age_recipient": recipient,
            "private_media_status": manifest.get("private_media_status"),
            "private_media_file_count": manifest.get("private_media_file_count"),
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=1).encode()
        need = cipher_bytes + len(sidecar)

        delete, after = plan_with_proven_survivors(gh, gens, need, watermark, budget, min_keep)
        if after + need > budget:
            raise BackupError(
                f"новая копия ({need // MIB} МиБ) не помещается в бюджет {budget // MIB} МиБ "
                f"даже после ротации: занято {after // MIB} МиБ; ничего не удалено")
        for gen in delete:
            log(f"ротация: удаляю {gen['kind']} {gen['tag']} ({gen['bytes']} байт)")
            gh.delete_release(gen["id"])
        gens = [g for g in gens if g not in delete]

        body = (f"{MARKER}\nРезервная копия DenisStock {run}.\n"
                f"Зашифровано age. Размер {cipher_bytes} байт, SHA-256 {cipher_sha}.\n")
        release = gh.create_draft(tag, body)
        if not release.get("draft"):
            raise BackupError("GitHub создал выпуск не черновиком: останавливаюсь")
        asset = gh.upload(release["id"], cipher_name, cipher)
        if asset.get("name") != cipher_name or asset.get("size") != cipher_bytes:
            raise BackupError("GitHub сохранил файл с другим именем или размером")
        size, sha = read_back(gh, asset["id"], what=cipher_name)
        if (size, sha) != (cipher_bytes, cipher_sha):
            raise BackupError("чтение назад не совпало: копия на GitHub не принята")
        sidecar_path = work / sidecar_name
        sidecar_path.write_bytes(sidecar)
        meta = gh.upload(release["id"], sidecar_name, sidecar_path)
        if read_back(gh, meta["id"], what=sidecar_name) != (
                len(sidecar), hashlib.sha256(sidecar).hexdigest()):
            raise BackupError("чтение назад описания копии не совпало")
        published = gh.publish(release["id"])
        if published.get("draft") or published.get("tag_name") != tag:
            raise BackupError("выпуск не опубликован")
        gens = inventory(gh)
        mine = [g for g in gens if g["tag"] == tag]
        if not mine or mine[0]["kind"] != "valid":
            raise BackupError("после публикации копия не распознана как целая")
        # The new copy is proven now: bring usage back under the watermark, still
        # keeping at least ``min_keep`` valid generations (survivors re-proven).
        # Also make room for TOMORROW's copy now, while the new copy is proven:
        # in the steady state nothing is deleted before an upload.
        reserve = int(cipher_bytes * float(conf.get("GHB_RESERVE_FACTOR", "1.1")))
        delete, _ = plan_with_proven_survivors(gh, gens, reserve, watermark, budget, min_keep)
        for gen in delete:
            log(f"ротация после выгрузки: удаляю {gen['kind']} {gen['tag']}")
            gh.delete_release(gen["id"])
        if delete:
            gens = inventory(gh)
        state = "ok" if total_bytes(gens) <= watermark else "warning"
        message = (f"Копия {run} зашифрована, выгружена на GitHub и прочитана назад"
                   + ("" if state == "ok" else "; занято больше рабочего порога"))
        status.write(state, message, ciphertext_sha256=cipher_sha, ciphertext_bytes=cipher_bytes,
                     **usage(gens, budget, watermark))
        log(message)
        return 0
    except BackupError as exc:
        status.write("failed", str(exc))
        log(f"ОШИБКА: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - any surprise must end as "failed", not "running"
        status.write("failed", f"внутренняя ошибка {type(exc).__name__}: требуется разбор журнала")
        log(f"ВНУТРЕННЯЯ ОШИБКА {type(exc).__name__}")
        import traceback

        traceback.print_exc()
        return 1
    except Stop:
        status.write("failed",
                     "Запуск прерван сигналом; черновик будет удален при следующем запуске")
        log("прерван сигналом")
        return 1
    finally:
        if cipher is not None:
            for path in (cipher, cipher.with_suffix(cipher.suffix + ".partial")):
                path.unlink(missing_ok=True)
        if work is not None and work.exists():
            for path in work.glob("*.backup.json"):
                path.unlink()
        lock.close()


def usage(gens, budget, watermark):
    valid = [g for g in gens if g["kind"] == "valid"]
    return {"used_bytes": total_bytes(gens), "budget_bytes": budget,
            "watermark_bytes": watermark, "valid_generations": len(valid),
            "newest_valid_run": max((g["run"] for g in valid), default=""),
            "oldest_valid_run": min((g["run"] for g in valid), default="")}


# --- check (monitor, run away from the VPS) ---------------------------------------


def check(conf, args) -> int:
    budget = min(int(conf.get("GHB_BUDGET_BYTES", str(1024 * MIB))), 1024 * MIB)
    try:
        gh = GitHub(conf, read_token(conf))
        gens = inventory(gh)
    except BackupError as exc:
        print(f"UNREACHABLE {exc}")
        return 1
    valid = [g for g in gens if g["kind"] == "valid"]
    invalid = [g for g in gens if g["kind"] == "invalid"]
    used = total_bytes(gens)
    summary = f"valid={len(valid)} invalid={len(invalid)} used={used} budget={budget}"
    if used > budget:
        print(f"OVER_BUDGET {summary}")
        return 1
    published = [g for g in gens if g["kind"] in ("valid", "invalid")]
    if published and published[-1]["kind"] == "invalid":
        print(f"CORRUPT newest {published[-1]['tag']}: {published[-1].get('reason')}; {summary}")
        return 1
    if not valid:
        print(f"STALE no valid generation; {summary}")
        return 2
    newest = valid[-1]
    if args.deep:
        targets = valid if args.deep_all else [newest]
        for gen in targets:
            sidecar = gen["sidecar"]
            size, sha = read_back(gh, gen["cipher_asset"]["id"], what=gen["tag"])
            if (size, sha) != (sidecar["ciphertext_bytes"], sidecar["ciphertext_sha256"]):
                print(f"CORRUPT {gen['tag']}: SHA-256 does not match; {summary}")
                return 1
    created = _parse_time(newest.get("created_at") or "")
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600 if created else 1e9
    line = f"{newest['run']} age={age_hours:.1f}h {summary}"
    if age_hours > args.max_age_hours:
        print(f"STALE {line}")
        return 2
    if len(valid) < 2:
        print(f"DEGRADED {line} (fewer than two valid generations)")
        return 2
    print(f"FRESH {line}" + (" INTACT" if args.deep else ""))
    return 0


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- fetch (recovery with GitHub alone) -------------------------------------------


def fetch(conf, args) -> int:
    out = Path(args.out)
    gh = GitHub(conf, read_token(conf))
    valid = [g for g in inventory(gh) if g["kind"] == "valid"]
    if args.tag:
        valid = [g for g in valid if g["tag"] == args.tag]
    if not valid:
        raise BackupError("на GitHub нет подходящей целой копии")
    gen = valid[-1]
    sidecar = gen["sidecar"]
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = out / sidecar["ciphertext_name"]
    if target.exists():
        raise BackupError(f"{target} уже существует: не перезаписываю")
    partial = target.with_suffix(target.suffix + ".partial")
    digest = hashlib.sha256()
    with partial.open("xb") as fh:
        def sink(chunk):
            digest.update(chunk)
            fh.write(chunk)
        size = gh.download(gen["cipher_asset"]["id"], sink, what=gen["tag"])
    if (size, digest.hexdigest()) != (sidecar["ciphertext_bytes"], sidecar["ciphertext_sha256"]):
        partial.unlink()
        raise BackupError(f"скачанная копия {gen['tag']} повреждена: SHA-256 не совпадает")
    partial.replace(target)
    (out / f"{sidecar['run']}.backup.json").write_text(json.dumps(sidecar, indent=1))
    print(json.dumps({"tag": gen["tag"], "file": str(target), "bytes": size,
                      "sha256": digest.hexdigest(), "created_at": sidecar.get("created_at")}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("push")
    chk = sub.add_parser("check")
    chk.add_argument("--max-age-hours", type=float, default=26)
    chk.add_argument("--deep", action="store_true", help="download and compare SHA-256")
    chk.add_argument("--deep-all", action="store_true", help="with --deep: every valid copy")
    fet = sub.add_parser("fetch")
    fet.add_argument("--out", required=True)
    fet.add_argument("--tag")
    args = parser.parse_args(argv)

    def stop(signum, frame):  # noqa: ARG001
        raise Stop()
    signal.signal(signal.SIGTERM, stop)
    os.umask(0o077)
    conf = load_config()
    try:
        if args.command == "push":
            return push(conf)
        if args.command == "check":
            return check(conf, args)
        return fetch(conf, args)
    except BackupError as exc:
        log(f"ОШИБКА: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"ВНУТРЕННЯЯ ОШИБКА {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
