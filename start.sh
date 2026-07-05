#!/usr/bin/env bash
set -euo pipefail
echo "=== Запуск DenisStock ==="
docker compose up -d --build
echo "DenisStock запущен: http://localhost"
