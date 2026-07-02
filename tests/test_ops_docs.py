"""v1.1.9 — production runbook + restore compatibility docs.

Лёгкие тесты: только наличие/содержание docs и валидность ссылок README. Без Docker/
rclone/сети/VPS.
"""
import re
from pathlib import Path

from django.conf import settings

BASE = Path(settings.BASE_DIR)
RUNBOOK = BASE / "docs" / "operations" / "production-deploy-runbook.md"
CHECKLIST = BASE / "docs" / "operations" / "post-deploy-checklist.md"
INCIDENT = (
    BASE / "docs" / "operations" / "incidents"
    / "2026-07-02-pg-restore-transaction-timeout.md"
)
PLAN37 = BASE / "docs" / "plans" / "37-postgres-backup-restore-version-compatibility.md"


def test_ops_docs_exist():
    for path in (RUNBOOK, CHECKLIST, INCIDENT, PLAN37):
        assert path.exists(), path


def test_runbook_mentions_safe_commands():
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in ("restore_db", "--yes", "ops_check", "ufw", "docker compose up -d --build"):
        assert needle in text


def test_checklist_has_items():
    text = CHECKLIST.read_text(encoding="utf-8")
    assert "- [ ]" in text
    assert "offsite" in text.lower()


def test_incident_mentions_transaction_timeout():
    text = INCIDENT.read_text(encoding="utf-8")
    assert "transaction_timeout" in text
    assert "postgres:16" in text


def test_plan37_covers_versions():
    text = PLAN37.read_text(encoding="utf-8")
    assert "postgres:16" in text
    assert "postgresql-client-16" in text


def test_readme_doc_links_resolve():
    readme = (BASE / "README.md").read_text(encoding="utf-8")
    for rel in re.findall(r"\]\((docs/[^)#]+)\)", readme):
        assert (BASE / rel).exists(), rel


def test_docs_have_no_obvious_secrets():
    for path in (RUNBOOK, CHECKLIST, INCIDENT, PLAN37):
        lowered = path.read_text(encoding="utf-8").lower()
        assert "private key" not in lowered
        assert "aws_secret_access_key" not in lowered
        assert "begin rsa" not in lowered
