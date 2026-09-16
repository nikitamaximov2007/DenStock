#!/usr/bin/python3
"""MAX V1 production release steps. Small, separate, auditable; never a deploy by itself.

Run on the production host as root from the release checkout (/opt/denstock).
Each subcommand does one thing and says what it did. Nothing prints a secret.

    preflight        read-only gates: base SHA, clean tree, writers, edge, secret files
    install-secrets  write .env.max (token, hidden prompt) and .env.max-webhook (generated)
    set-username     give catalog-web the public MAX bot username (from max_bot_identity)
    unset-username   remove it again (rollback: the handoff shows its fallback)
    edge-install     put the MAX webhook route into the live Caddyfile, validate, reload
    edge-rollback    restore the pre-MAX Caddyfile, validate, reload
    public-role      re-run the canonical public role script after migrations
    verify           read-only gates after the release: code, migrations, health, webhook
    plan             print the exact release and rollback sequence

Steps that change something require --execute; without it they only check and
say what they would do. The service rollout itself stays in plain, visible
`docker compose` commands printed by `plan`.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import ssl
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path("/opt/denstock")
LIVE_CADDYFILE = Path("/etc/denstock/caddy/Caddyfile")
CA_DIR = Path("/etc/denstock/max")
CA_NAME = "russian-trusted-root-ca.pem"
PRE_MAX_CADDY_SHA256 = "ff363d12176b1da8a939f06fca9d8198cb614dc87d592b58368a5cc38d0f0340"
PUBLIC_ROLE = "denstock_public"
WEBHOOK_PATH = "/customer-requests/max/webhook/"
PUBLIC_WEBHOOK_URL = f"https://pro-brp.ru{WEBHOOK_PATH}"
SIGNING_OVERLAY = "docker-compose.signing.yml"
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{2,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{5,256}$")
WRITER_PATTERNS = (
    "pg_dump", "pg_restore", "backup_all", "denstock-backup", "backup_offsite",
    "manage.py migrate", "manage.py restore", "git checkout", "docker compose up",
    "docker compose build",
)
COMPOSE_PROFILES = ("--profile", "public-catalog", "--profile", "telegram-bot",
                    "--profile", "max-bot")


class ReleaseError(RuntimeError):
    """A gate failed or a step could not complete; the message is safe to print."""


@dataclass
class Context:
    root: Path = ROOT
    caddyfile: Path = LIVE_CADDYFILE
    ca_dir: Path = CA_DIR
    execute: bool = False
    require_root: bool = True
    out: list = field(default_factory=list)
    run: object = None
    prompt: object = None
    sleep: object = time.sleep
    # Rehearsal only (local simulation): alternate Caddyfiles standing for the
    # production pair, e.g. the same files switched to plain HTTP.
    pre_max_caddyfile: Path | None = None
    candidate_caddyfile: Path | None = None

    def say(self, text: str) -> None:
        self.out.append(text)
        print(text, flush=True)

    def sh(self, argv, *, input_text=None, check=True):
        if self.run is not None:
            result = self.run(list(argv), input_text)
        else:
            result = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                                    cwd=self.root)
        if check and result.returncode != 0:
            raise ReleaseError(f"command failed ({result.returncode}): {' '.join(argv[:6])}")
        return result


def compose(*args):
    return ["docker", "compose", *COMPOSE_PROFILES, *args]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def env_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            values[key.strip()] = value.strip().strip("'\"")
    return values


def _require_root(ctx: Context) -> None:
    if ctx.require_root and os.geteuid() != 0:
        raise ReleaseError("run as root on the production host")


def _write_private(path: Path, text: str) -> None:
    """Create or replace a root-only file: 0600, written in full before it is visible."""
    partial = path.with_name(path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, path)


def write_in_place(path: Path, text: str) -> None:
    """Rewrite a file keeping its inode, mode and owner.

    A single-file Docker bind mount (the proxy's Caddyfile) follows the inode:
    replacing the file by rename would leave the running container reading the
    old one, and a reload would silently keep the old routing.
    """
    with open(path, "r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write(text)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def ca_fingerprint(path: Path) -> str:
    text = path.read_text(encoding="ascii")
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----\s+.+?-----END CERTIFICATE-----", text,
                        re.DOTALL)
    if len(blocks) != 1:
        raise ReleaseError(f"{path} must contain exactly one certificate")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(blocks[0])).hexdigest()


def _mode(path: Path) -> str:
    info = path.stat()
    return f"{info.st_uid}:{info.st_gid} {stat.S_IMODE(info.st_mode):o}"


# --- preflight ---------------------------------------------------------------------------


def preflight(ctx: Context, *, expect_head: str, candidate: str = "") -> dict:
    """Read-only gates before the first production write."""
    failures = []
    report = {}
    if not SHA_RE.fullmatch(expect_head) or (candidate and not SHA_RE.fullmatch(candidate)):
        raise ReleaseError("SHAs must be full 40-character lowercase hex")
    head = ctx.sh(["git", "rev-parse", "HEAD"]).stdout.strip()
    report["head"] = head
    if head != expect_head:
        failures.append(f"HEAD is {head}, expected {expect_head}")
    status = ctx.sh(["git", "status", "--porcelain"]).stdout.splitlines()
    unexpected = [line for line in status if line.strip() != f"?? {SIGNING_OVERLAY}"]
    report["dirty"] = unexpected
    if unexpected:
        failures.append(f"working tree not clean: {unexpected}")
    env = env_values(ctx.root / ".env")
    report["app_commit"] = env.get("DENSTOCK_APP_COMMIT", "")
    if env.get("DENSTOCK_APP_COMMIT") != head:
        failures.append("DENSTOCK_APP_COMMIT does not match HEAD")
    if SIGNING_OVERLAY not in env.get("COMPOSE_FILE", ""):
        failures.append("COMPOSE_FILE lost the signing overlay")
    if not (ctx.root / SIGNING_OVERLAY).is_file():
        failures.append("signing overlay file missing")
    for key in ("MAX_BOT_TOKEN", "MAX_WEBHOOK_SECRET"):
        if key in env:
            failures.append(f"{key} must not be in the shared .env")
    if candidate:
        known = ctx.sh(["git", "cat-file", "-e", f"{candidate}^{{commit}}"], check=False)
        report["candidate_fetched"] = known.returncode == 0
        if known.returncode != 0:
            failures.append(f"candidate {candidate} not fetched")
    processes = ctx.sh(["ps", "-eo", "pid=,args="]).stdout.splitlines()
    writers = [
        line.strip() for line in processes
        if any(pattern in line for pattern in WRITER_PATTERNS) and "max_release" not in line
    ]
    report["writers"] = writers
    if writers:
        failures.append(f"another writer is active: {writers}")
    if ctx.caddyfile.is_file():
        live = sha256_file(ctx.caddyfile)
        report["caddyfile"] = (
            "pre-max" if live == _pre_max_sha(ctx)
            else "candidate" if live == sha256_file(_candidate_path(ctx))
            else "unknown"
        )
        if report["caddyfile"] == "unknown":
            failures.append(f"live Caddyfile {live} is neither pre-max nor candidate")
    else:
        failures.append("live Caddyfile missing")
    for name in (".env.max", ".env.max-webhook"):
        path = ctx.root / name
        report[name] = _mode(path) if path.exists() else "absent"
        if path.exists() and not report[name].endswith(" 600"):
            failures.append(f"{name} must be mode 600")
    ca = ctx.ca_dir / CA_NAME
    report["ca_file"] = ca_fingerprint(ca) if ca.is_file() else "absent"
    for key, value in report.items():
        ctx.say(f"{key}: {value}")
    if failures:
        for failure in failures:
            ctx.say(f"FAIL {failure}")
        raise ReleaseError(f"preflight failed: {len(failures)} gate(s)")
    ctx.say("PREFLIGHT PASS")
    return report


# --- secrets -----------------------------------------------------------------------------


def install_secrets(ctx: Context, *, ca_sha256: str, public_url: str = PUBLIC_WEBHOOK_URL,
                    replace: bool = False) -> None:
    """Write .env.max and .env.max-webhook; the token is read without echo."""
    _require_root(ctx)
    max_file, hook_file = ctx.root / ".env.max", ctx.root / ".env.max-webhook"
    existing = [path.name for path in (max_file, hook_file) if path.exists()]
    if existing and not replace:
        raise ReleaseError(f"{existing} already exist; use --replace to rotate them")
    if not public_url.startswith("https://") or not public_url.endswith(WEBHOOK_PATH):
        raise ReleaseError("public webhook URL must be https://<host>" + WEBHOOK_PATH)
    ca = ctx.ca_dir / CA_NAME
    expected = re.sub(r"[^0-9a-f]", "", ca_sha256.lower())
    if len(expected) != 64:
        raise ReleaseError("--ca-sha256 must be the 64-hex SHA-256 of the MAX root CA")
    if not ca.is_file():
        raise ReleaseError(f"place the official CA certificate at {ca} first")
    if ca_fingerprint(ca) != expected:
        raise ReleaseError("CA certificate does not match --ca-sha256")
    if not ctx.execute:
        ctx.say(f"would write {max_file.name} and {hook_file.name} (0600); rerun with --execute")
        return
    prompt = ctx.prompt or getpass.getpass
    token = prompt("MAX bot token (input hidden): ").strip()
    if not token or len(token) > 512 or any(char.isspace() for char in token):
        raise ReleaseError("token is empty or malformed; nothing written")
    if ctx.prompt is not _stdin_token and token != prompt(
        "Repeat the token (input hidden): "
    ).strip():
        raise ReleaseError("the two entries differ; nothing written")
    webhook_secret = secrets.token_urlsafe(48)
    assert SECRET_RE.fullmatch(webhook_secret)
    _write_private(
        max_file,
        f"MAX_BOT_TOKEN={token}\n"
        f"MAX_PUBLIC_WEBHOOK_URL={public_url}\n"
        f"MAX_API_CA_FILE=/etc/denstock/max/{CA_NAME}\n"
        f"MAX_API_CA_SHA256={expected}\n",
    )
    _write_private(
        hook_file, f"MAX_WEBHOOK_ENABLED=true\nMAX_WEBHOOK_SECRET={webhook_secret}\n"
    )
    del token, webhook_secret
    ctx.say(f"wrote {max_file.name} (4 keys) and {hook_file.name} (2 keys), mode 600")


def _stdin_token(_label: str) -> str:
    """Rehearsal only: one line from stdin, never echoed."""
    return sys.stdin.readline()


def set_username(ctx: Context, *, username: str) -> None:
    if username.startswith("MAX_BOT_USERNAME="):
        username = username.split("=", 1)[1]
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise ReleaseError("not a usable MAX bot username")
    _set_public_key(ctx, "MAX_BOT_USERNAME", username)


def unset_username(ctx: Context) -> None:
    _set_public_key(ctx, "MAX_BOT_USERNAME", None)


def _set_public_key(ctx: Context, key: str, value: str | None) -> None:
    path = ctx.root / ".env.public"
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.strip().startswith(f"{key}=")]
    previous = [line.split("=", 1)[1] for line in lines if line.strip().startswith(f"{key}=")]
    if value is not None:
        kept.append(f"{key}={value}")
    ctx.say(f"{path.name}: {key} {previous or 'unset'} -> {value or 'unset'}")
    if not ctx.execute:
        ctx.say("dry run; rerun with --execute")
        return
    write_in_place(path, "\n".join(kept) + "\n")


# --- edge --------------------------------------------------------------------------------


def _caddy_in_proxy(ctx: Context, action: str) -> None:
    ctx.sh(compose("exec", "-T", "proxy", "caddy", action, "--config", "/etc/caddy/Caddyfile",
                   "--adapter", "caddyfile"))


def _swap_caddyfile(ctx: Context, *, text: str, expect_live: set[str], label: str) -> None:
    _require_root(ctx)
    live = sha256_file(ctx.caddyfile)
    target = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if live == target:
        ctx.say(f"live Caddyfile already {label} ({target[:12]}); nothing to do")
        return
    if live not in expect_live:
        raise ReleaseError(f"live Caddyfile {live[:12]} is not the expected state; stop")
    ctx.say(f"live Caddyfile {live[:12]} -> {label} {target[:12]}")
    if not ctx.execute:
        ctx.say("dry run; rerun with --execute")
        return
    previous = ctx.caddyfile.read_text(encoding="utf-8")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = ctx.caddyfile.with_name(f"Caddyfile.before-{label}-{stamp}")
    backup.write_text(previous, encoding="utf-8")
    os.chmod(backup, 0o640)
    write_in_place(ctx.caddyfile, text)
    try:
        running = ctx.sh(compose("exec", "-T", "proxy", "sha256sum", "/etc/caddy/Caddyfile"))
        if running.stdout.split()[:1] != [target]:
            raise ReleaseError("proxy container does not see the new Caddyfile (bind mount)")
        _caddy_in_proxy(ctx, "validate")
        _caddy_in_proxy(ctx, "reload")
    except ReleaseError:
        write_in_place(ctx.caddyfile, previous)
        ctx.sh(compose("exec", "-T", "proxy", "caddy", "reload", "--config",
                       "/etc/caddy/Caddyfile", "--adapter", "caddyfile"), check=False)
        ctx.say(f"restored the previous Caddyfile (copy kept at {backup.name})")
        raise
    ctx.say(f"Caddyfile is {label}; previous copy {backup.name}")


def _candidate_path(ctx: Context) -> Path:
    return ctx.candidate_caddyfile or ctx.root / "deploy/caddy/Caddyfile.production"


def _pre_max_path(ctx: Context) -> Path:
    return ctx.pre_max_caddyfile or ctx.root / "deploy/caddy/Caddyfile.production.pre-max"


def _pre_max_sha(ctx: Context) -> str:
    if ctx.pre_max_caddyfile is not None:
        return sha256_file(ctx.pre_max_caddyfile)
    return PRE_MAX_CADDY_SHA256


def edge_install(ctx: Context) -> None:
    text = _candidate_path(ctx).read_text(encoding="utf-8")
    _swap_caddyfile(ctx, text=text, expect_live={_pre_max_sha(ctx)}, label="max")


def edge_rollback(ctx: Context) -> None:
    text = _pre_max_path(ctx).read_text(encoding="utf-8")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != _pre_max_sha(ctx):
        raise ReleaseError("repository pre-max copy is not the recorded production file")
    candidate = sha256_file(_candidate_path(ctx))
    _swap_caddyfile(ctx, text=text, expect_live={candidate}, label="pre-max")


# --- database role -----------------------------------------------------------------------


def public_role(ctx: Context, *, role: str = PUBLIC_ROLE) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", role):
        raise ReleaseError("unsafe role name")
    script = (ctx.root / "scripts/operations/create_public_catalog_role.sql").read_text()
    ctx.say(f"re-run create_public_catalog_role.sql for {role} (idempotent)")
    if not ctx.execute:
        ctx.say("dry run; rerun with --execute")
        return
    ctx.sh(
        compose("exec", "-T", "db", "sh", "-c",
                f'psql -v ON_ERROR_STOP=1 -v public_role={role} -q '
                '-U "$POSTGRES_USER" -d "$POSTGRES_DB"'),
        input_text=script,
    )
    ctx.say("public role refreshed")


# --- verify ------------------------------------------------------------------------------

EXPECTED_MIGRATIONS = (
    ("customer_requests", "0008_max_messaging"),
    ("customer_requests", "0009_max_public_link_guard"),
    ("operations", "0006_max_messaging"),
)
CODE_MARKERS = {
    "web": ("/app/apps/customer_requests/views.py", "def max_webhook"),
    "catalog-web": ("/app/config/public_urls.py", "public_catalog_max_continue"),
    "telegram-bot": ("/app/apps/customer_requests/telegram_bot.py",
                     "def send_max_operator_deliveries"),
    "max-bot": ("/app/apps/customer_requests/max_bot.py", "def health_problems"),
}


def verify(ctx: Context, *, candidate: str, services: list[str], require_subscribed: bool):
    """Read-only gates after the rollout, for the services already rolled out."""
    unknown = [service for service in services if service not in CODE_MARKERS]
    if unknown:
        raise ReleaseError(f"unknown services: {unknown}")
    failures = []
    head = ctx.sh(["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != candidate:
        failures.append(f"HEAD {head} is not the candidate")
    if env_values(ctx.root / ".env").get("DENSTOCK_APP_COMMIT") != candidate:
        failures.append("DENSTOCK_APP_COMMIT is not the candidate")
    check = ctx.sh(compose("exec", "-T", "web", "python", "manage.py", "migrate", "--check"),
                   check=False)
    if check.returncode != 0:
        failures.append("unapplied migrations")
    shown = ctx.sh(compose("exec", "-T", "web", "python", "manage.py", "showmigrations",
                           "customer_requests", "operations")).stdout
    for _app, name in EXPECTED_MIGRATIONS:
        if f"[X] {name}" not in shown:
            failures.append(f"migration {name} not applied")
    states = {}
    for line in ctx.sh(compose("ps", "--format", "json")).stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        states[item.get("Service")] = (item.get("State"), item.get("Health"))
    for service in services:
        state = states.get(service)
        if state != ("running", "healthy"):
            failures.append(f"{service} is {state}")
            continue
        path, marker = CODE_MARKERS[service]
        found = ctx.sh(compose("exec", "-T", service, "grep", "-q", marker, path), check=False)
        if found.returncode != 0:
            failures.append(f"{service} does not run the candidate code")
    if "max-bot" in services:
        health = ctx.sh(compose("exec", "-T", "max-bot", "python", "manage.py", "max_bot_health"),
                        check=False)
        if health.returncode != 0:
            failures.append("max_bot_health failed")
    if require_subscribed:
        status = ctx.sh(compose("run", "--rm", "--no-deps", "max-bot", "max_webhook", "status",
                                "--require-subscribed"), check=False)
        if status.returncode != 0:
            failures.append("MAX webhook not subscribed to the configured URL")
    for failure in failures:
        ctx.say(f"FAIL {failure}")
    if failures:
        raise ReleaseError(f"verify failed: {len(failures)} gate(s)")
    ctx.say("VERIFY PASS")


# --- plan --------------------------------------------------------------------------------

PLAN_TEMPLATE = Path(__file__).with_name("max_release_plan.txt")


def plan_text(*, base: str, candidate: str) -> str:
    if not SHA_RE.fullmatch(base) or not SHA_RE.fullmatch(candidate):
        raise ReleaseError("SHAs must be full 40-character lowercase hex")
    return PLAN_TEMPLATE.read_text(encoding="utf-8").format(
        base=base,
        candidate=candidate,
        c=" ".join(COMPOSE_PROFILES),
        CA_NAME=CA_NAME,
        PUBLIC_WEBHOOK_URL=PUBLIC_WEBHOOK_URL,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--caddyfile", type=Path, default=LIVE_CADDYFILE)
    parser.add_argument("--ca-dir", type=Path, default=CA_DIR)
    parser.add_argument(
        "--rehearsal", action="store_true",
        help="local simulation only: no root check, alternate Caddyfiles, token on stdin",
    )
    parser.add_argument("--pre-max-caddyfile", type=Path)
    parser.add_argument("--candidate-caddyfile", type=Path)
    parser.add_argument("--token-from-stdin", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("preflight")
    pre.add_argument("--expect-head", required=True)
    pre.add_argument("--candidate", default="")
    sec = commands.add_parser("install-secrets")
    sec.add_argument("--ca-sha256", required=True)
    sec.add_argument("--public-url", default=PUBLIC_WEBHOOK_URL)
    sec.add_argument("--replace", action="store_true")
    sec.add_argument("--execute", action="store_true")
    user = commands.add_parser("set-username")
    user.add_argument("username")
    user.add_argument("--execute", action="store_true")
    unset = commands.add_parser("unset-username")
    unset.add_argument("--execute", action="store_true")
    for name in ("edge-install", "edge-rollback"):
        commands.add_parser(name).add_argument("--execute", action="store_true")
    role = commands.add_parser("public-role")
    role.add_argument("--role", default=PUBLIC_ROLE)
    role.add_argument("--execute", action="store_true")
    ver = commands.add_parser("verify")
    ver.add_argument("--candidate", required=True)
    ver.add_argument("--services", default="web,telegram-bot")
    ver.add_argument("--require-subscribed", action="store_true")
    plan = commands.add_parser("plan")
    plan.add_argument("--base", required=True)
    plan.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    rehearsal_only = [
        name for name in ("pre_max_caddyfile", "candidate_caddyfile", "token_from_stdin")
        if getattr(args, name)
    ]
    if rehearsal_only and not args.rehearsal:
        print(f"STOP: {rehearsal_only} are rehearsal-only options", file=sys.stderr)
        return 1
    ctx = Context(root=args.root, caddyfile=args.caddyfile, ca_dir=args.ca_dir,
                  execute=getattr(args, "execute", False), require_root=not args.rehearsal,
                  pre_max_caddyfile=args.pre_max_caddyfile,
                  candidate_caddyfile=args.candidate_caddyfile,
                  prompt=_stdin_token if args.token_from_stdin else None)
    try:
        if args.command == "preflight":
            preflight(ctx, expect_head=args.expect_head, candidate=args.candidate)
        elif args.command == "install-secrets":
            install_secrets(ctx, ca_sha256=args.ca_sha256, public_url=args.public_url,
                            replace=args.replace)
        elif args.command == "set-username":
            set_username(ctx, username=args.username)
        elif args.command == "unset-username":
            unset_username(ctx)
        elif args.command == "edge-install":
            edge_install(ctx)
        elif args.command == "edge-rollback":
            edge_rollback(ctx)
        elif args.command == "public-role":
            public_role(ctx, role=args.role)
        elif args.command == "verify":
            verify(ctx, candidate=args.candidate,
                   services=[item for item in args.services.split(",") if item],
                   require_subscribed=args.require_subscribed)
        elif args.command == "plan":
            print(plan_text(base=args.base, candidate=args.candidate))
    except ReleaseError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
