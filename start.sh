#!/usr/bin/env bash
set -euo pipefail
echo "=== Запуск DenStock ==="
docker compose up -d --build
echo "DenStock запущен: http://localhost"
