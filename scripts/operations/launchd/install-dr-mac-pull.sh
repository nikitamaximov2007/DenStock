#!/usr/bin/env bash
# Render the LaunchAgent into ~/Library/LaunchAgents. Does NOT load it and
# never touches credentials: the operator reviews the plist, then runs
# `launchctl bootstrap gui/$(id -u) <plist>` themselves.
set -euo pipefail
: "${PYTHON:?}" "${REPO:?}" "${YANDEX_REMOTE:?}" "${GOOGLE_REMOTE:?}"
: "${BACKUP_DIRECTORY:?}" "${PINNED_PUBLIC_KEY:?}" "${RCLONE_CONFIG:?}"
LOG_DIRECTORY="${LOG_DIRECTORY:-$HOME/Library/Logs/DenisStock}"
OUT="${OUT:-$HOME/Library/LaunchAgents/com.denstock.dr-mac-pull.plist}"
mkdir -p "$LOG_DIRECTORY" "$BACKUP_DIRECTORY"
chmod 700 "$BACKUP_DIRECTORY"
export LOG_DIRECTORY
python3 - "$OUT" <<'PY'
import os, pathlib, sys
src = pathlib.Path(os.environ["REPO"]) / "scripts/operations/launchd/com.denstock.dr-mac-pull.plist.in"
text = src.read_text()
for placeholder, env in {
    "__PYTHON__": "PYTHON", "__REPO__": "REPO",
    "__YANDEX_READ_ONLY_REMOTE__": "YANDEX_REMOTE",
    "__GOOGLE_READ_ONLY_REMOTE__": "GOOGLE_REMOTE",
    "__BACKUP_DIRECTORY__": "BACKUP_DIRECTORY",
    "__PINNED_PUBLIC_KEY__": "PINNED_PUBLIC_KEY",
    "__RCLONE_CONFIG__": "RCLONE_CONFIG", "__LOG_DIRECTORY__": "LOG_DIRECTORY",
}.items():
    text = text.replace(placeholder, os.environ[env])
pathlib.Path(sys.argv[1]).write_text(text)
PY
plutil -lint "$OUT"
echo "Rendered $OUT (not loaded)."
