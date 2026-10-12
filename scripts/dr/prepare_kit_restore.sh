#!/usr/bin/env bash
# Turn an encrypted recovery kit into the inputs of offline_restore.sh.
#
#   prepare_kit_restore.sh --kit <dir with *.age> --identity <recovery-identity.age>
#                          --work <NEW empty dir> [--secrets <production-secrets-*.tar.age>]
#                          [--plain-identity]
#
# The passphrase is typed into age on the terminal (never an argument, never
# echoed); the unlocked identity lives only in a shell variable and reaches age
# through a /dev/fd pipe. Decrypted data is written ONLY under --work, which
# must not exist yet and is created 0700. --plain-identity is for synthetic
# tests with an unprotected test key. Nothing here talks to any server.
set -euo pipefail
umask 077
KIT="" IDENTITY="" WORK="" PLAIN=0 SECRETS_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kit) KIT="$2"; shift 2 ;;
        --identity) IDENTITY="$2"; shift 2 ;;
        --work) WORK="$2"; shift 2 ;;
        --secrets) SECRETS_ARG="$2"; shift 2 ;;
        --plain-identity) PLAIN=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
die() { echo "[kit] STOP: $*" >&2; exit 1; }
[[ -d "$KIT" && -f "$IDENTITY" && -n "$WORK" ]] || die "--kit, --identity and --work are required"
[[ ! -e "$WORK" ]] || die "$WORK already exists: use a new directory"
pick() { local found; found=$(ls -1 "$KIT"/$1 2>/dev/null | sort | tail -1); [[ -n "$found" ]] || die "kit has no $1"; printf '%s' "$found"; }
BACKUP=$(pick '*.signed-backup.tar.age')
PRIVATE=$(pick 'private_media.*.tar.age')
INVENTORY="$PRIVATE.inventory.age"; [[ -f "$INVENTORY" ]] || die "missing $INVENTORY"
SECRETS="$SECRETS_ARG"
if [[ -z "$SECRETS" ]]; then
    SECRETS=$(ls -1 "$KIT"/production-secrets-*.tar.age 2>/dev/null | sort | tail -1 || true)
fi
[[ -f "$SECRETS" ]] || die "no secrets archive: pass --secrets <production-secrets-*.tar.age>"
BUNDLE=$(pick 'source-code.*.bundle.age')

if [[ "$PLAIN" == 1 ]]; then
    ID="$(cat "$IDENTITY")"
else
    echo "Введите парольную фразу ключа восстановления:" >&2
    ID="$(age -d "$IDENTITY" </dev/tty)" || die "the identity did not unlock (wrong passphrase?)"
fi
dec() { age -d -i <(printf '%s\n' "$ID") "$1"; }
# Prove the identity opens this kit BEFORE anything is written.
dec "$INVENTORY" >/dev/null 2>&1 || { unset ID; die "this identity cannot decrypt the kit"; }

mkdir -m 700 "$WORK" "$WORK/run" "$WORK/secrets" "$WORK/env"
echo "[kit] decrypt the signed backup run" >&2
dec "$BACKUP" | tar -xf - -C "$WORK/run" --strip-components=1
echo "[kit] decrypt private_media and its inventory" >&2
dec "$PRIVATE" > "$WORK/private.tar"
dec "$INVENTORY" > "$WORK/private-inventory.json"
echo "[kit] decrypt configuration and secrets (files stay 0600 under work)" >&2
dec "$SECRETS" | tar -xf - -C "$WORK/secrets"
echo "[kit] decrypt the source bundle and check out the backup's commit" >&2
dec "$BUNDLE" > "$WORK/source.bundle"
unset ID

python3 - "$WORK" <<'PY'
import json, os, subprocess, sys
from pathlib import Path
work = Path(sys.argv[1])
manifest = json.loads((work / "run" / "manifest.json").read_text())
commit = manifest["app_commit"]
subprocess.run(["git", "clone", "-q", str(work / "source.bundle"), str(work / "source")], check=True)
subprocess.run(["git", "-C", str(work / "source"), "checkout", "-q", "--detach", commit], check=True)
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
pub = secrets / "etc/denstock/manifest-signing/production-ed25519.pub"
(work / "pinned-public.pem").write_bytes(pub.read_bytes())
print(json.dumps({"backup_created_at": manifest["created_at"], "app_commit": commit[:12],
                  "key_id": manifest["signature"]["key_id"]}))
PY
echo "[kit] ready. Next (isolated test):" >&2
echo "  scripts/dr/offline_restore.sh --project dr-test-<date> --source $WORK/source --run $WORK/run \\" >&2
echo "    --private-archive $WORK/private.tar --private-inventory $WORK/private-inventory.json \\" >&2
echo "    --env-dir $WORK/env --pubkey $WORK/pinned-public.pem --port 18080 --mode test --report $WORK/report.json" >&2
echo "  then: scripts/dr/cleanup_dr.sh dr-test-<date>; rm -rf $WORK" >&2
