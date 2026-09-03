"""Regression coverage for the fresh-machine development entrypoint."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_agent_entrypoint_links_the_standalone_guides():
    content = (ROOT / "AGENT-BOOTSTRAP.md").read_text(encoding="utf-8")
    assert "new-machine-bootstrap.md" in content
    assert "disaster-recovery.md" in content
    assert "production" in content.lower()


def test_bootstrap_script_generates_development_only_configuration():
    content = (ROOT / "scripts" / "bootstrap-new-machine.ps1").read_text(encoding="utf-8")
    assert "DJANGO_SETTINGS_MODULE=config.settings.dev" in content
    assert "DENSTOCK_MODE=development" in content
    assert "DATABASE_URL" not in content
    assert "token_urlsafe(48)" in content


def test_bootstrap_script_refuses_unsafe_and_missing_prerequisites():
    content = (ROOT / "scripts" / "bootstrap-new-machine.ps1").read_text(encoding="utf-8")
    assert "Refusing a production-like target" in content
    assert "Install Git for Windows" in content
    assert "Python 3.12+ is required" in content
    assert "RequireDocker" in content


def test_bootstrap_script_never_requests_or_prints_production_secrets():
    content = (ROOT / "scripts" / "bootstrap-new-machine.ps1").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD" not in content
    assert "DENSTOCK_DR_S3_SECRET_KEY" not in content
    assert "Set-ExecutionPolicy -Scope Process" not in content
