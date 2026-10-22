#!/usr/bin/env bash
# Restore DenisStock onto NEW, isolated infrastructure from independent copies.
#
# Never touches an existing database, volume or the production host: the
# compose project must start with "dr-", may not already own any Docker volume,
# and input paths under /opt/denstock or /var/lib/docker are refused. Inputs are
# DECRYPTED copies the operator prepared (see docs/operations/disaster-recovery
# -vps-and-yandex-lost.md); this script never decrypts and never reads secrets
# into its own arguments or output.
#
#   offline_restore.sh --project dr-test-20261013 --source <checkout at app_commit>
#     --run <backup run dir> [--private-archive <tar or tar.gz>]
#     [--private-inventory <json rows>] --env-dir <db.env web.env public.env>
#     --pubkey <pinned ed25519 public pem> --port 18080 --mode test|disaster
#     --report <report.json>
set -euo pipefail

PROJECT="" SOURCE="" RUN="" PRIVATE="" INVENTORY="" ENV_DIR="" PUBKEY="" PORT="" MODE="" REPORT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project) PROJECT="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        --run) RUN="$2"; shift 2 ;;
        --private-archive) PRIVATE="$2"; shift 2 ;;
        --private-inventory) INVENTORY="$2"; shift 2 ;;
        --env-dir) ENV_DIR="$2"; shift 2 ;;
        --pubkey) PUBKEY="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --report) REPORT="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

die() { echo "[dr] STOP: $*" >&2; exit 1; }
step() { echo "[dr] $(date -u +%H:%M:%S) $*"; }
STARTED=$(date -u +%s)

[[ "$PROJECT" =~ ^dr-[a-z0-9-]{1,40}$ ]] || die "project must look like dr-<name>"
[[ "$MODE" == test || "$MODE" == disaster ]] || die "--mode test|disaster is required"
[[ "$PORT" =~ ^[0-9]{4,5}$ ]] || die "--port is required"
for path in "$SOURCE" "$RUN" "$ENV_DIR" "$PUBKEY" "$REPORT"; do
    [[ -n "$path" ]] || die "missing required path argument"
done
for path in "$SOURCE" "$RUN" "$ENV_DIR" "${PRIVATE:-}"; do
    [[ -z "$path" ]] && continue
    real="$(cd "$(dirname "$path")" && pwd -P)/$(basename "$path")"
    case "$real" in /opt/denstock*|/var/lib/docker*) die "refusing production path $real" ;; esac
done
for f in db.env web.env public.env; do [[ -f "$ENV_DIR/$f" ]] || die "missing $ENV_DIR/$f"; done
for f in manifest.json db.dump; do [[ -f "$RUN/$f" ]] || die "missing $RUN/$f"; done
if docker volume ls --format '{{.Name}}' | grep -q "^${PROJECT}_"; then
    die "volumes of $PROJECT already exist: recovery only into NEW resources"
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then die "port $PORT is busy"; fi

PY="${DR_PYTHON:-python3}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
export DR_PROJECT="$PROJECT" DR_SOURCE="$(cd "$SOURCE" && pwd -P)" DR_ENV_DIR="$(cd "$ENV_DIR" && pwd -P)" DR_PORT="$PORT" DR_TOOL_DIR="$HERE"
COMPOSE=(docker compose -f "$HERE/compose.dr.yml" -p "$PROJECT")

step "1/9 verify the signed backup (SHA-256 + Ed25519) and the source commit"
"$PY" - "$RUN" "$PUBKEY" "$DR_SOURCE" <<'PY'
import base64, hashlib, json, subprocess, sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
run, pubkey, source = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
m = json.loads((run / "manifest.json").read_text())
if m.get("private_media_status") == "included":
    # private_media.tar.gz travels inside the run: it must be signed and present.
    name = m.get("private_media_filename") or ""
    if name not in m["sha256"] or not (run / name).is_file():
        sys.exit("manifest says private_media is included, but its archive is missing")
for name, digest in m["sha256"].items():
    h = hashlib.sha256((run / name).read_bytes()).hexdigest()
    if h != digest:
        sys.exit(f"SHA-256 mismatch for {name}")
payload = json.dumps({k: v for k, v in m.items() if k != "signature"}, ensure_ascii=True,
                     separators=(",", ":"), sort_keys=True).encode("ascii")
key = serialization.load_pem_public_key(open(pubkey, "rb").read())
key.verify(base64.b64decode(m["signature"]["value"]), payload)
head = subprocess.run(["git", "-C", source, "rev-parse", "HEAD"], capture_output=True,
                      text=True, check=True).stdout.strip()
if head != m["app_commit"]:
    sys.exit(f"source HEAD {head} != backup app_commit {m['app_commit']}")
print(f"  signed by {m['signature']['key_id']}, created {m['created_at']}, commit {head[:12]}")
PY

step "2/9 build the application image from the verified source"
"${COMPOSE[@]}" build web >/dev/null

step "3/9 start a NEW PostgreSQL 16 and create the public catalog role"
"${COMPOSE[@]}" up -d --wait db >/dev/null
# The role password is read from public.env by Python and sent over stdin, never argv.
"$PY" - "$DR_ENV_DIR/public.env" <<'PY' | "${COMPOSE[@]}" exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null
import sys
from urllib.parse import unquote, urlparse
env = dict(line.split("=", 1) for line in open(sys.argv[1]).read().splitlines()
           if "=" in line and not line.startswith("#"))
url = urlparse(env["PUBLIC_DATABASE_URL"].strip())
role, password = unquote(url.username), unquote(url.password or "")
assert role.replace("_", "").isalnum()
print(f"CREATE ROLE {role} LOGIN PASSWORD '{password.replace(chr(39), chr(39) * 2)}';")
PY

step "4/9 restore the database dump into the empty database"
"${COMPOSE[@]}" exec -T db sh -c 'pg_restore --no-owner --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$RUN/db.dump"
# Role session defaults (read-only, timeouts) are cluster-level: not in pg_dump.
"${COMPOSE[@]}" exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$DR_SOURCE/scripts/operations/create_public_catalog_role.sql" >/dev/null

step "5/9 restore public media and private_media into NEW volumes (owner = image user)"
if [[ -f "$RUN/media.tar.gz" ]]; then
    "${COMPOSE[@]}" run --rm --no-deps -T --entrypoint sh web -c 'tar -xzf - -C /app/mediafiles' \
        < "$RUN/media.tar.gz"
fi
mkdir -p "$HERE/.dr-tmp-$PROJECT"; trap 'rm -rf "$HERE/.dr-tmp-$PROJECT"' EXIT
STRIP=0
if [[ -n "$PRIVATE" ]]; then
    STRIP=$("$PY" - "$PRIVATE" <<'PY'
import sys, tarfile
with tarfile.open(sys.argv[1]) as t:
    tops = {m.name.split("/")[0] for m in t if m.name not in (".", "")}
print(1 if tops == {"_data"} else 0)
PY
)
    "${COMPOSE[@]}" run --rm --no-deps -T --entrypoint sh web -c \
        "tar -xf - --strip-components=$STRIP -C /app/private_media" < "$PRIVATE"
elif [[ -f "$RUN/private_media.tar.gz" ]]; then
    "${COMPOSE[@]}" run --rm --no-deps -T --entrypoint sh web -c \
        'tar -xzf - -C /app/private_media' < "$RUN/private_media.tar.gz"
fi

step "6/9 start web (entrypoint migrate must be a no-op) and the public catalog"
"${COMPOSE[@]}" up -d --wait web catalog-web proxy >/dev/null
MIGRATE_LOG="$("${COMPOSE[@]}" logs web 2>&1 | grep -c 'No migrations to apply' || true)"

step "7/9 validate data, files and references inside the restored web"
"${COMPOSE[@]}" exec -T web mkdir -p /tmp/dr
"${COMPOSE[@]}" cp "$RUN/manifest.json" web:/tmp/dr/manifest.json >/dev/null
if [[ -n "$INVENTORY" ]]; then
    "${COMPOSE[@]}" cp "$INVENTORY" web:/tmp/dr/private-inventory.json >/dev/null
fi
VALIDATION="$("${COMPOSE[@]}" exec -T web python manage.py shell < "$HERE/validate_restore.py" 2>/dev/null \
    | grep '^DR-VALIDATION ' | sed 's/^DR-VALIDATION //')"
[[ -n "$VALIDATION" ]] || die "validator produced no report"

step "8/9 HTTP checks through the loopback proxy and network isolation"
JAR="$HERE/.dr-tmp-$PROJECT/cookies"
BASE="http://127.0.0.1:$PORT"
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }
HTTP="{}"
HTTP=$("$PY" -c 'import json,sys; print(json.dumps({}))')
add() { HTTP=$("$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); d[sys.argv[2]]=sys.argv[3]; print(json.dumps(d))' "$HTTP" "$1" "$2"); }
add healthz "$(code "$BASE/healthz/")"
add login_page "$(code "$BASE/login/")"
add catalog_home "$(code -H 'Host: catalog.localhost' "$BASE/")"
add catalog_search "$(code -H 'Host: catalog.localhost' "$BASE/search/?q=test")"
add private_media_denied "$(code "$BASE/media/customer_requests/any-file.png")"
FIRST_MEDIA="$("${COMPOSE[@]}" exec -T web sh -c 'cd /app/mediafiles && find . -type f ! -path "./customer_requests/*" | head -1' | sed 's#^\./##')"
if [[ -n "$FIRST_MEDIA" ]]; then
    add public_media_file "$(code "$BASE/media/$("$PY" -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$FIRST_MEDIA")")"
fi
if [[ "$MODE" == test ]]; then
    # A throwaway account in the DISPOSABLE test database proves the login flow.
    export DR_CHECK_PASSWORD="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    "${COMPOSE[@]}" exec -T -e DR_CHECK_PASSWORD web python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
get_user_model().objects.create_superuser('dr-check', '', os.environ['DR_CHECK_PASSWORD'])
" >/dev/null 2>&1
    curl -s -c "$JAR" -b "$JAR" -o /dev/null "$BASE/login/"
    CSRF=$(awk '$6=="csrftoken"{print $7}' "$JAR")
    LOGIN=$(curl -s -c "$JAR" -b "$JAR" -o /dev/null -w '%{http_code}' -H "Referer: $BASE/login/" \
        --data-urlencode "csrfmiddlewaretoken=$CSRF" --data-urlencode "username=dr-check" \
        --data-urlencode "password=$DR_CHECK_PASSWORD" "$BASE/login/")
    unset DR_CHECK_PASSWORD
    add login_post "$LOGIN"
    for page in warehouse:/warehouse/ customers:/sales/customers/ history:/inventory/movements/ inventory:/inventory/balance/ search:/search/?q=test; do
        add "page_${page%%:*}" "$(code -b "$JAR" "$BASE${page#*:}")"
    done
fi
EGRESS=$("${COMPOSE[@]}" exec -T web python -c "
import socket
try:
    socket.create_connection(('1.1.1.1', 443), timeout=3).close(); print('OPEN')
except OSError:
    print('BLOCKED')" 2>/dev/null | tail -1)
add web_egress "$EGRESS"

step "9/9 report"
ELAPSED=$(( $(date -u +%s) - STARTED ))
"$PY" - "$REPORT" "$PROJECT" "$MODE" "$ELAPSED" "$MIGRATE_LOG" "$VALIDATION" "$HTTP" <<'PY'
import json, sys
path, project, mode, elapsed, migrate_log, validation, http = sys.argv[1:8]
validation, http = json.loads(validation), json.loads(http)
expected = {"healthz": "200", "login_page": "200", "catalog_home": "200",
            "catalog_search": "200", "private_media_denied": "404", "web_egress": "BLOCKED"}
if "public_media_file" in http:
    expected["public_media_file"] = "200"
if mode == "test":
    expected.update({"login_post": "302", "page_warehouse": "200", "page_customers": "200",
                     "page_history": "200", "page_inventory": "200", "page_search": "200"})
http_ok = all(http.get(k) == v for k, v in expected.items())
report = {"project": project, "mode": mode, "elapsed_seconds": int(elapsed),
          "migrate_was_noop": migrate_log != "0", "validation": validation, "http": http,
          "http_passed": http_ok,
          "passed": validation["passed"] and http_ok and migrate_log != "0"}
open(path, "w").write(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))
print(json.dumps({k: report[k] for k in ("project", "mode", "elapsed_seconds", "passed",
                                          "http_passed", "migrate_was_noop")}))
PY
