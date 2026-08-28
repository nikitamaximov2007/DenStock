#!/usr/bin/env bash
# Install only when explicitly run by an operator on a host. Never invoked by deploy.
set -euo pipefail
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/denstock-backup-capped"
TARGET="/usr/local/sbin/denstock-backup-capped"
[ "$(id -u)" -eq 0 ] || { echo 'Run as root.' >&2; exit 2; }
bash -n "$SOURCE"
staged="$(mktemp "${TARGET}.new.XXXXXX")"
trap 'rm -f "$staged"' EXIT
install -o root -g root -m 0755 "$SOURCE" "$staged"
if [ -e "$TARGET" ]; then cp -p "$TARGET" "${TARGET}.bak.$(date -u +%Y%m%dT%H%M%SZ)"; fi
mv -f "$staged" "$TARGET"
trap - EXIT
echo "Installed $TARGET. Roll back: cp -p ${TARGET}.bak.<timestamp> $TARGET"
