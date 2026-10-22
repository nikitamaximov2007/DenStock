#!/usr/bin/env bash
# Turn an encrypted recovery kit into the inputs of offline_restore.sh.
#
#   prepare_kit_restore.sh --kit <dir with *.age> --identity <recovery-identity.age>
#                          --work <NEW empty dir> [--secrets <production-secrets-*.tar.age>]
#                          [--plain-identity]
#                          [--fresh-secrets --pubkey <pinned production public key>]
#                          [--source-git <url or path of the source repository>]
#
# Recovery from GitHub alone: --fresh-secrets writes NEW random env files
# (scripts/dr/fresh_env.py) instead of reading a secrets archive, --pubkey is
# the owner's separately kept copy of the production signing PUBLIC key, and
# --source-git clones the code instead of decrypting a source bundle.
#
# The passphrase is typed into age on the terminal (never an argument, never
# echoed); the unlocked identity lives only in a shell variable and reaches age
# through a /dev/fd pipe. Decrypted data is written ONLY under --work, which
# must not exist yet and is created 0700. --plain-identity is for synthetic
# tests with an unprotected test key. Nothing here talks to any server.
set -euo pipefail
umask 077
KIT="" IDENTITY="" WORK="" PLAIN=0 SECRETS_ARG="" FRESH=0 PUBKEY_ARG="" SOURCE_GIT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kit) KIT="$2"; shift 2 ;;
        --identity) IDENTITY="$2"; shift 2 ;;
        --work) WORK="$2"; shift 2 ;;
        --secrets) SECRETS_ARG="$2"; shift 2 ;;
        --plain-identity) PLAIN=1; shift ;;
        --fresh-secrets) FRESH=1; shift ;;
        --pubkey) PUBKEY_ARG="$2"; shift 2 ;;
        --source-git) SOURCE_GIT="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
die() { echo "[kit] STOP: $*" >&2; exit 1; }
[[ -d "$KIT" && -f "$IDENTITY" && -n "$WORK" ]] || die "--kit, --identity and --work are required"
[[ ! -e "$WORK" ]] || die "$WORK already exists: use a new directory"
pick() { local found; found=$(ls -1 "$KIT"/$1 2>/dev/null | sort | tail -1); [[ -n "$found" ]] || die "kit has no $1"; printf '%s' "$found"; }
BACKUP=$(pick '*.signed-backup.tar.age')
# A separate private_media capture is optional: runs made by the backup-only
# release (and pushed by independent_offsite_push.sh) carry private_media.tar.gz
# inside the signed run instead.
PRIVATE=$(ls -1 "$KIT"/private_media.*.tar.age 2>/dev/null | sort | tail -1 || true)
INVENTORY=""
if [[ -n "$PRIVATE" ]]; then
    INVENTORY="$PRIVATE.inventory.age"; [[ -f "$INVENTORY" ]] || die "missing $INVENTORY"
fi
HERE="$(cd "$(dirname "$0")" && pwd -P)"
SECRETS=""
if [[ "$FRESH" == 1 ]]; then
    [[ -f "$PUBKEY_ARG" ]] || die "--fresh-secrets needs --pubkey <pinned production public key>"
    [[ -z "$SECRETS_ARG" ]] || die "--fresh-secrets and --secrets exclude each other"
else
    SECRETS="$SECRETS_ARG"
    if [[ -z "$SECRETS" ]]; then
        SECRETS=$(ls -1 "$KIT"/production-secrets-*.tar.age 2>/dev/null | sort | tail -1 || true)
    fi
    [[ -f "$SECRETS" ]] || die "no secrets archive: pass --secrets <production-secrets-*.tar.age> or --fresh-secrets"
fi
BUNDLE=""
[[ -n "$SOURCE_GIT" ]] || BUNDLE=$(pick 'source-code.*.bundle.age')

if [[ "$PLAIN" == 1 ]]; then
    ID="$(cat "$IDENTITY")"
else
    echo "Введите парольную фразу ключа восстановления:" >&2
    ID="$(age -d "$IDENTITY" </dev/tty)" || die "the identity did not unlock (wrong passphrase?)"
fi
dec() { age -d -i <(printf '%s\n' "$ID") "$1"; }
# Prove the identity opens this kit BEFORE anything is written (header only).
PROBE="${INVENTORY:-$BACKUP}"
# head closes the pipe early, so age's exit status is meaningless here: count bytes.
PROBED=$(dec "$PROBE" 2>/dev/null | head -c 64 | wc -c | tr -d ' ' || true)
[[ "$PROBED" -gt 0 ]] || { unset ID; die "this identity cannot decrypt the kit"; }

mkdir -m 700 "$WORK" "$WORK/run" "$WORK/secrets" "$WORK/env"
echo "[kit] decrypt the signed backup run" >&2
dec "$BACKUP" | tar -xf - -C "$WORK/run" --strip-components=1
if [[ -n "$PRIVATE" ]]; then
    echo "[kit] decrypt private_media and its inventory" >&2
    dec "$PRIVATE" > "$WORK/private.tar"
    dec "$INVENTORY" > "$WORK/private-inventory.json"
elif [[ ! -f "$WORK/run/private_media.tar.gz" ]]; then
    unset ID; die "no private_media: neither a separate capture nor one inside the run"
fi
if [[ -n "$SECRETS" ]]; then
    echo "[kit] decrypt configuration and secrets (files stay 0600 under work)" >&2
    dec "$SECRETS" | tar -xf - -C "$WORK/secrets"
fi
if [[ -n "$BUNDLE" ]]; then
    echo "[kit] decrypt the source bundle and check out the backup's commit" >&2
    dec "$BUNDLE" > "$WORK/source.bundle"
fi
unset ID

python3 - "$WORK" "$SOURCE_GIT" "$FRESH" "$PUBKEY_ARG" "$HERE/fresh_env.py" <<'PY'
import json, os, re, shutil, subprocess, sys
from pathlib import Path
work, source_git, fresh, pubkey_arg, fresh_env = sys.argv[1:6]
work = Path(work)
manifest = json.loads((work / "run" / "manifest.json").read_text())
commit = manifest["app_commit"]
if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
    sys.exit("STOP: backup manifest does not contain a full source Git SHA")
origin = source_git or str(work / "source.bundle")
source = str(work / "source")
# An ordinary clone fetches all advertised branches. Make that explicit: the
# deployed commit can still live on a release branch while main catches up.
subprocess.run(["git", "clone", "-q", "--no-checkout", "--no-single-branch",
                origin, source], check=True)
present = subprocess.run(["git", "-C", source, "cat-file", "-e", f"{commit}^{{commit}}"],
                         capture_output=True)
if present.returncode:
    sys.exit("STOP: source Git SHA is absent from the supplied repository. "
             "Use the full GitHub source URL, not a shallow/single-branch checkout; "
             "preserve the deployed commit on a remote branch or tag.")
subprocess.run(["git", "-C", source, "checkout", "-q", "--detach", commit], check=True)
head = subprocess.run(["git", "-C", source, "rev-parse", "HEAD"], check=True,
                      capture_output=True, text=True).stdout.strip()
if head != commit:
    sys.exit("STOP: restored source HEAD differs from the signed backup Git SHA")
if fresh == "1":
    os.rmdir(work / "env")
    subprocess.run([sys.executable, fresh_env, str(work / "env"), commit], check=True)
    shutil.copyfile(pubkey_arg, work / "pinned-public.pem")
    print(json.dumps({"backup_created_at": manifest["created_at"], "app_commit": commit[:12],
                      "key_id": manifest["signature"]["key_id"], "secrets": "fresh"}))
    sys.exit(0)
secrets = work / "secrets"
env_main = (secrets / "opt/denstock/.env").read_text()
keys = dict(line.split("=", 1) for line in env_main.splitlines()
            if "=" in line and not line.lstrip().startswith("#"))
def write(name, text):
    path = work / "env" / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
write("db.env", "".join(f"{k}={keys[k]}\n" for k in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")))
write("web.env", env_main)
write("public.env", (secrets / "opt/denstock/.env.public").read_text())
pub = Path(pubkey_arg) if pubkey_arg else secrets / "etc/denstock/manifest-signing/production-ed25519.pub"
(work / "pinned-public.pem").write_bytes(pub.read_bytes())
print(json.dumps({"backup_created_at": manifest["created_at"], "app_commit": commit[:12],
                  "key_id": manifest["signature"]["key_id"]}))
PY
PRIVATE_ARGS=""
[[ -f "$WORK/private.tar" ]] && PRIVATE_ARGS="--private-archive $WORK/private.tar --private-inventory $WORK/private-inventory.json"
echo "[kit] ready. Next (isolated test):" >&2
echo "  scripts/dr/offline_restore.sh --project dr-test-<date> --source $WORK/source --run $WORK/run \\" >&2
echo "    $PRIVATE_ARGS \\" >&2
echo "    --env-dir $WORK/env --pubkey $WORK/pinned-public.pem --port 18080 --mode test --report $WORK/report.json" >&2
echo "  then: scripts/dr/cleanup_dr.sh dr-test-<date>; rm -rf $WORK" >&2
