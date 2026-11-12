"""Guard the release's default process budget on the 2 GiB VPS."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_web_and_catalog_default_to_one_threaded_worker_without_disabling_healthchecks():
    dockerfile = (ROOT / "docker/Dockerfile").read_text()
    command = next(
        json.loads(line.removeprefix("CMD "))
        for line in dockerfile.splitlines() if line.startswith("CMD [")
    )
    assert command[command.index("--workers") + 1] == "1"
    assert command[command.index("--threads") + 1] == "4"
    assert command[command.index("--worker-class") + 1] == "gthread"

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    catalog_command = services["catalog-web"]["command"]
    assert "--workers=${CATALOG_WEB_WORKERS:-1}" in catalog_command
    assert "--threads=${CATALOG_WEB_THREADS:-4}" in catalog_command
    for name in ("web", "catalog-web", "db", "telegram-bot", "max-bot"):
        assert services[name]["healthcheck"]["test"]
