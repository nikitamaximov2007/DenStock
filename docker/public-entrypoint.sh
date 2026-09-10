#!/usr/bin/env bash
# Public runtime never migrates or creates users: its DB role is SELECT-only.
set -euo pipefail

echo "[public-entrypoint] checking database readiness…"
python - <<'PY'
import os, time
import psycopg

dsn = os.environ.get("PUBLIC_DATABASE_URL", "")
for _ in range(60):
    try:
        psycopg.connect(dsn, connect_timeout=2).close()
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("[public-entrypoint] database is unavailable")
PY

exec "$@"
