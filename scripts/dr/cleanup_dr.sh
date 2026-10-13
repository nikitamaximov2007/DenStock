#!/usr/bin/env bash
# Remove ONE disposable recovery stack: containers, its own volumes and image.
# Refuses anything whose project name does not start with "dr-".
set -euo pipefail
PROJECT="${1:-}"
[[ "$PROJECT" =~ ^dr-[a-z0-9-]{1,40}$ ]] || { echo "usage: cleanup_dr.sh dr-<name>" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd -P)"
export DR_PROJECT="$PROJECT" DR_SOURCE="${DR_SOURCE:-$HERE/../..}" DR_ENV_DIR="${DR_ENV_DIR:-/nonexistent}" DR_PORT="${DR_PORT:-1}"
mkdir -p /tmp/dr-empty-env && touch /tmp/dr-empty-env/{db,web,public}.env
DR_ENV_DIR=/tmp/dr-empty-env docker compose -f "$HERE/compose.dr.yml" -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 || true
docker image rm "${PROJECT}-app:local" >/dev/null 2>&1 || true
left="$(docker volume ls --format '{{.Name}}' | grep "^${PROJECT}_" || true)"
[[ -z "$left" ]] || { echo "volumes still present: $left" >&2; exit 1; }
echo "removed $PROJECT"
