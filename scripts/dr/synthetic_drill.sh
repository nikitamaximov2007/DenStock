#!/usr/bin/env bash
# End-to-end recovery drill with SYNTHETIC data only:
#   synthetic production -> signed backup + private_media capture -> destroy it
#   -> offline_restore.sh into brand-new resources -> validation -> cleanup.
# No production data, no production secrets, no network access for the apps.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
SOURCE="$(cd "$HERE/../.." && pwd -P)"
PY="${DR_PYTHON:-python3}"
STAMP="$(date -u +%Y%m%d%H%M%S)"
SRC="dr-src-$STAMP"
DST="dr-drill-$STAMP"
PORT="${DR_PORT:-18087}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dr-drill.XXXX")"
chmod 700 "$WORK"
cleanup() {
    "$HERE/cleanup_dr.sh" "$SRC" >/dev/null 2>&1 || true
    if [[ "${KEEP_DRILL:-0}" != 1 ]]; then "$HERE/cleanup_dr.sh" "$DST" >/dev/null 2>&1 || true; fi
    [[ -n "${FAKE_PID:-}" ]] && kill "$FAKE_PID" 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT
echo "[drill] work dir $WORK, projects $SRC -> $DST"

# --- synthetic secrets (random, never production) --------------------------------
"$PY" "$HERE/fresh_env.py" "$WORK/env" "$(git -C "$SOURCE" rev-parse HEAD)"
"$PY" - "$WORK" <<'PY'
import sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
key = Ed25519PrivateKey.generate()
work = Path(sys.argv[1])
(work / "signing.pem").write_bytes(key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
(work / "signing.pub.pem").write_bytes(key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
PY

export DR_SOURCE="$SOURCE" DR_ENV_DIR="$WORK/env" DR_PORT="$((PORT + 1))"
SRC_COMPOSE=(docker compose -f "$HERE/compose.dr.yml" -p "$SRC")

# --- 1. synthetic production -----------------------------------------------------
echo "[drill] build and start synthetic production"
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" build web >/dev/null
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" up -d --wait db >/dev/null
"$PY" - "$WORK/env/public.env" <<'PY' | DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" exec -T db sh -c 'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null
import sys
from urllib.parse import urlparse
env = dict(l.split("=", 1) for l in open(sys.argv[1]).read().splitlines() if "=" in l)
u = urlparse(env["PUBLIC_DATABASE_URL"])
print(f"CREATE ROLE {u.username} LOGIN PASSWORD '{u.password}';")
PY
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" up -d --wait web >/dev/null
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$SOURCE/scripts/operations/create_public_catalog_role.sql" >/dev/null

echo "[drill] seed synthetic business data, public media and private_media"
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" exec -T web python manage.py shell < "$HERE/seed_synthetic.py" | tail -1
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" cp "$WORK/signing.pem" web:/tmp/signing.pem >/dev/null
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" exec -T -u 0 web chown 2001 /tmp/signing.pem

echo "[drill] signed backup_all (production mode, synthetic key)"
RUN_LINE="$(DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" exec -T \
    -e DENSTOCK_MANIFEST_SIGNING_KEY_PATH=/tmp/signing.pem -e DENSTOCK_MANIFEST_SIGNING_KEY_ID=dr-drill \
    web python manage.py backup_all --trigger manual | tail -1)"
RUN_ID="$(printf '%s' "$RUN_LINE" | sed -nE 's#^.*backups/([0-9_-]+).*$#\1#p')"
[[ -n "$RUN_ID" ]] || { echo "no run id in: $RUN_LINE" >&2; exit 1; }
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" cp "web:/app/backups/$RUN_ID" "$WORK/run" >/dev/null
# private_media captured like the real kit: a tar of the volume rooted at _data/
DR_PROJECT="$SRC" "${SRC_COMPOSE[@]}" exec -T web sh -c \
    'mkdir -p /tmp/cap && ln -sfn /app/private_media /tmp/cap/_data && tar -C /tmp/cap -chf - _data' > "$WORK/private.tar"
"$PY" - "$WORK/private.tar" "$WORK/private-inventory.json" <<'PY'
import hashlib, json, sys, tarfile
rows = []
with tarfile.open(sys.argv[1]) as t:
    for m in t:
        if m.isreg():
            data = t.extractfile(m).read()
            rows.append([m.name.split("/", 1)[1], hashlib.sha256(data).hexdigest(), len(data)])
json.dump(rows, open(sys.argv[2], "w"))
print(f"[drill] private capture: {len(rows)} files")
PY

# INRUN=1: the backup-only release format. private_media.tar.gz travels INSIDE
# the signed run (this is what independent_offsite_push.sh uploads), there is
# no separate capture and no inventory: the signed tree hash is the check.
if [[ "${INRUN:-0}" == 1 ]]; then
    "$PY" - "$WORK/private.tar" "$WORK/run" "$WORK/signing.pem" <<'PY'
import base64, gzip, hashlib, io, json, sys, tarfile
from pathlib import Path
from cryptography.hazmat.primitives import serialization
src, run, key = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
rows, buf = [], io.BytesIO()
with tarfile.open(src) as t, tarfile.open(fileobj=buf, mode="w") as out:
    for m in t:
        if not m.isreg():
            continue
        data = t.extractfile(m).read()
        rel = m.name.split("/", 1)[1]
        rows.append((tuple(rel.split("/")), rel, hashlib.sha256(data).hexdigest(), len(data)))
        info = tarfile.TarInfo("./" + rel); info.size = len(data); info.mode = 0o644
        out.addfile(info, io.BytesIO(data))
archive = run / "private_media.tar.gz"
archive.write_bytes(gzip.compress(buf.getvalue()))
digest = hashlib.sha256()
for _, rel, h, _ in sorted(rows):
    digest.update(len(rel.encode()).to_bytes(8, "big")); digest.update(rel.encode())
    digest.update(bytes.fromhex(h))
if not rows:
    digest.update(b"empty-media-tree\n")
m = json.loads((run / "manifest.json").read_text()); m.pop("signature")
sha = hashlib.sha256(archive.read_bytes()).hexdigest()
m.update(private_media_status="included", private_media_filename=archive.name,
         private_media_sha256=sha, private_media_tree_sha256=digest.hexdigest(),
         private_media_file_count=len(rows), private_media_bytes=sum(r[3] for r in rows))
m["sha256"][archive.name] = sha
pk = serialization.load_pem_private_key(open(key, "rb").read(), None)
payload = json.dumps(m, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
m["signature"] = {"algorithm": "ed25519", "key_id": "dr-drill",
                  "value": base64.b64encode(pk.sign(payload)).decode()}
(run / "manifest.json").write_text(json.dumps(m))
print(f"[drill] private_media inside the signed run: {len(rows)} files")
PY
    rm -f "$WORK/private.tar" "$WORK/private-inventory.json"
fi

# --- optional fault injection: the restore must REFUSE or REPORT FAILURE --------
case "${DRILL_FAULT:-}" in
    "") ;;
    tamper_dump) printf 'X' | dd of="$WORK/run/db.dump" bs=1 seek=4096 conv=notrunc 2>/dev/null ;;
    foreign_key) "$PY" - "$WORK" <<'PY'
import sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
other = Ed25519PrivateKey.generate().public_key()
Path(sys.argv[1], "signing.pub.pem").write_bytes(other.public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
PY
        ;;
    wrong_commit) "$PY" - "$WORK/run/manifest.json" "$WORK/signing.pem" <<'PY'
import base64, json, sys
from cryptography.hazmat.primitives import serialization
path, key = sys.argv[1], serialization.load_pem_private_key(open(sys.argv[2], "rb").read(), None)
m = json.load(open(path)); m["app_commit"] = "0" * 40; m.pop("signature")
payload = json.dumps(m, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
m["signature"] = {"algorithm": "ed25519", "key_id": "dr-drill",
                  "value": base64.b64encode(key.sign(payload)).decode()}
json.dump(m, open(path, "w"))
PY
        ;;
    drop_private_file) [[ "${INRUN:-0}" == 1 ]] && { printf 'X' | dd of="$WORK/run/private_media.tar.gz" bs=1 seek=40 conv=notrunc 2>/dev/null; } || "$PY" - "$WORK/private.tar" <<'PY'
import sys, tarfile
src = sys.argv[1]
members = []
with tarfile.open(src) as t:
    for m in t:
        if m.isreg() and m.name.endswith("drill-scan.pdf"):
            continue
        members.append((m, t.extractfile(m).read() if m.isreg() else None))
import io
with tarfile.open(src, "w") as out:
    for m, data in members:
        out.addfile(m, io.BytesIO(data) if data is not None else None)
PY
        ;;
    wrong_identity) ;;  # applied in KIT_MODE below
    *) echo "unknown DRILL_FAULT" >&2; exit 2 ;;
esac
[[ -n "${DRILL_FAULT:-}" ]] && echo "[drill] injected fault: $DRILL_FAULT"

# --- 2. disaster: production and its storage are gone ----------------------------
echo "[drill] destroy synthetic production (VPS and Yandex lost)"
"$HERE/cleanup_dr.sh" "$SRC" >/dev/null

# --- 3. recovery on new resources, from the captured files only -------------------
if [[ "${GITHUB:-0}" == 1 ]]; then
    # GitHub alone: the signed run (private_media inside, backup-only release)
    # goes through github_release_backup.py push to an ISOLATED fake GitHub; the
    # local run and every secret are then gone. Recovery uses only the release
    # asset fetched back, the source repository, the owner's age identity and
    # the separately kept signing PUBLIC key; secrets are generated fresh.
    "$PY" - "$WORK/run/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
sys.exit(0 if m.get("private_media_status") == "included"
         else "the run carries no private_media: GITHUB=1 needs the backup-only release")
PY
    rm -f "$WORK/private.tar" "$WORK/private-inventory.json"
    GH="$WORK/github"; mkdir -m 700 "$GH" "$GH/backups"
    age-keygen -o "$WORK/test-identity.txt" 2>/dev/null
    age-keygen -y "$WORK/test-identity.txt" > "$GH/recipient.txt"
    mv "$WORK/run" "$GH/backups/$RUN_ID"
    (cd "$SOURCE" && exec "$PY" -m tests.github_fake --serve "$GH/blobs" --port-file "$GH/port") &
    FAKE_PID=$!
    for _ in $(seq 1 100); do [[ -s "$GH/port" ]] && break; sleep 0.1; done
    (cd "$SOURCE" && "$PY" -c 'from tests.github_fake import WRITE_TOKEN as W, READ_TOKEN as R
import sys; open(sys.argv[1], "w").write(W); open(sys.argv[2], "w").write(R)' "$GH/write.token" "$GH/read.token")
    GHB=(env GHB_CONFIG=/nonexistent GHB_REPO=owner/denstock-backups
         GHB_API_URL="http://127.0.0.1:$(cat "$GH/port")"
         GHB_UPLOAD_URL="http://127.0.0.1:$(cat "$GH/port")/uploads"
         GHB_RECIPIENT_FILE="$GH/recipient.txt" BACKUP_ROOT="$GH/backups" GHB_LOCK_FILE="$GH/lock")
    "${GHB[@]}" GHB_TOKEN_FILE="$GH/write.token" "$PY" "$SOURCE/scripts/operations/github_release_backup.py" push
    echo "[drill] VPS lost: the local run is deleted; only GitHub remains"
    rm -rf "$GH/backups"
    KIT="$WORK/kit"
    "${GHB[@]}" GHB_TOKEN_FILE="$GH/read.token" "$PY" "$SOURCE/scripts/operations/github_release_backup.py" fetch --out "$KIT"
    if [[ "${DRILL_FAULT:-}" == wrong_identity ]]; then age-keygen -o "$WORK/test-identity.txt.other" 2>/dev/null; mv "$WORK/test-identity.txt.other" "$WORK/test-identity.txt"; fi
    "$HERE/prepare_kit_restore.sh" --kit "$KIT" --identity "$WORK/test-identity.txt" \
        --work "$WORK/prepared" --plain-identity --fresh-secrets --pubkey "$WORK/signing.pub.pem" \
        --source-git "$SOURCE"
    "$HERE/offline_restore.sh" --project "$DST" --source "$WORK/prepared/source" \
        --run "$WORK/prepared/run" --env-dir "$WORK/prepared/env" \
        --pubkey "$WORK/prepared/pinned-public.pem" --port "$PORT" --mode test \
        --report "${DR_REPORT:-$WORK/report.json}"
elif [[ "${KIT_MODE:-0}" == 1 ]]; then
    # Pack everything exactly like the real kit, encrypted to a synthetic age
    # identity, and recover only through prepare_kit_restore.sh.
    KIT="$WORK/kit"; mkdir -m 700 "$KIT"
    age-keygen -o "$WORK/test-identity.txt" 2>/dev/null
    RCPT="$(age-keygen -y "$WORK/test-identity.txt")"
    tar -C "$WORK" -cf - -s "#^run#$RUN_ID#" run | age -r "$RCPT" -o "$KIT/$RUN_ID.signed-backup.tar.age"
    if [[ -f "$WORK/private.tar" ]]; then
        age -r "$RCPT" -o "$KIT/private_media.drill.tar.age" "$WORK/private.tar"
        age -r "$RCPT" -o "$KIT/private_media.drill.tar.age.inventory.age" "$WORK/private-inventory.json"
    fi
    mkdir -p "$WORK/sec/opt/denstock" "$WORK/sec/etc/denstock/manifest-signing"
    cp "$WORK/env/web.env" "$WORK/sec/opt/denstock/.env"
    cp "$WORK/env/public.env" "$WORK/sec/opt/denstock/.env.public"
    cp "$WORK/signing.pub.pem" "$WORK/sec/etc/denstock/manifest-signing/production-ed25519.pub"
    tar -C "$WORK/sec" -cf - opt etc | age -r "$RCPT" -o "$KIT/production-secrets-drill.tar.age"
    git -C "$SOURCE" bundle create "$WORK/src.bundle" HEAD 2>/dev/null
    age -r "$RCPT" -o "$KIT/source-code.drill.bundle.age" "$WORK/src.bundle"
    rm -rf "$WORK/sec" "$WORK/src.bundle"
    if [[ "${DRILL_FAULT:-}" == wrong_identity ]]; then
        age-keygen -o "$WORK/test-identity.txt.other" 2>/dev/null
        mv "$WORK/test-identity.txt.other" "$WORK/test-identity.txt"
    fi
    "$HERE/prepare_kit_restore.sh" --kit "$KIT" --identity "$WORK/test-identity.txt" \
        --work "$WORK/prepared" --plain-identity
    PRIVATE_ARGS=()
    [[ -f "$WORK/prepared/private.tar" ]] && PRIVATE_ARGS=(--private-archive "$WORK/prepared/private.tar"
        --private-inventory "$WORK/prepared/private-inventory.json")
    "$HERE/offline_restore.sh" --project "$DST" --source "$WORK/prepared/source" \
        --run "$WORK/prepared/run" ${PRIVATE_ARGS[@]+"${PRIVATE_ARGS[@]}"} \
        --env-dir "$WORK/prepared/env" --pubkey "$WORK/prepared/pinned-public.pem" \
        --port "$PORT" --mode test --report "${DR_REPORT:-$WORK/report.json}"
else
    PRIVATE_ARGS=()
    [[ -f "$WORK/private.tar" ]] && PRIVATE_ARGS=(--private-archive "$WORK/private.tar"
        --private-inventory "$WORK/private-inventory.json")
    "$HERE/offline_restore.sh" --project "$DST" --source "$SOURCE" --run "$WORK/run" \
        ${PRIVATE_ARGS[@]+"${PRIVATE_ARGS[@]}"} \
        --env-dir "$WORK/env" --pubkey "$WORK/signing.pub.pem" --port "$PORT" --mode test \
        --report "${DR_REPORT:-$WORK/report.json}"
fi
if [[ -n "${DR_REPORT:-}" ]]; then echo "[drill] report: $DR_REPORT"; fi
