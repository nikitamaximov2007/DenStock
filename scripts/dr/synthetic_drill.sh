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
    rm -rf "$WORK"
}
trap cleanup EXIT
echo "[drill] work dir $WORK, projects $SRC -> $DST"

# --- synthetic secrets (random, never production) --------------------------------
"$PY" - "$WORK/env" "$(git -C "$SOURCE" rev-parse HEAD)" <<'PY'
import os, secrets, sys
out, commit = sys.argv[1], sys.argv[2]
os.makedirs(out, mode=0o700)
db_pw, pub_pw = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
def write(name, rows):
    path = os.path.join(out, name)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fh:
        fh.write("\n".join(f"{k}={v}" for k, v in rows) + "\n")
write("db.env", [("POSTGRES_DB", "denstock"), ("POSTGRES_USER", "denstock"), ("POSTGRES_PASSWORD", db_pw)])
write("web.env", [
    ("DJANGO_SETTINGS_MODULE", "config.settings.prod"), ("DJANGO_DEBUG", "false"),
    ("DJANGO_SECRET_KEY", secrets.token_urlsafe(40)), ("DJANGO_ALLOWED_HOSTS", "localhost"),
    ("POSTGRES_DB", "denstock"), ("POSTGRES_USER", "denstock"), ("POSTGRES_PASSWORD", db_pw),
    ("DATABASE_URL", f"postgres://denstock:{db_pw}@db:5432/denstock"),
    ("DENSTOCK_MODE", "production"), ("DENSTOCK_APP_COMMIT", commit),
    ("PRIVATE_MEDIA_ROOT", "/app/private_media"), ("TIME_ZONE", "Europe/Moscow"),
])
write("public.env", [
    ("DJANGO_SECRET_KEY", secrets.token_urlsafe(40)),
    ("PUBLIC_DATABASE_URL", f"postgres://denstock_public:{pub_pw}@db:5432/denstock"),
    ("DJANGO_SECURE_COOKIES", "false"),
])
PY
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
    drop_private_file) "$PY" - "$WORK/private.tar" <<'PY'
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
    *) echo "unknown DRILL_FAULT" >&2; exit 2 ;;
esac
[[ -n "${DRILL_FAULT:-}" ]] && echo "[drill] injected fault: $DRILL_FAULT"

# --- 2. disaster: production and its storage are gone ----------------------------
echo "[drill] destroy synthetic production (VPS and Yandex lost)"
"$HERE/cleanup_dr.sh" "$SRC" >/dev/null

# --- 3. recovery on new resources, from the captured files only -------------------
"$HERE/offline_restore.sh" --project "$DST" --source "$SOURCE" --run "$WORK/run" \
    --private-archive "$WORK/private.tar" --private-inventory "$WORK/private-inventory.json" \
    --env-dir "$WORK/env" --pubkey "$WORK/signing.pub.pem" --port "$PORT" --mode test \
    --report "${DR_REPORT:-$WORK/report.json}"
if [[ -n "${DR_REPORT:-}" ]]; then echo "[drill] report: $DR_REPORT"; fi
