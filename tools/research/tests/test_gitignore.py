from __future__ import annotations

from pathlib import Path


def test_gitignore_contains_research_safety_rules() -> None:
    project_root = Path(__file__).resolve().parents[3]
    gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")

    for pattern in (
        "research_inputs/",
        ".env.research.local",
        "!tools/research/.env.research.example",
        "*.session",
        "*.session-journal",
        "tools/research/.sessions/",
        "tools/research/.cache/",
        "tools/research/models/",
        "tools/research/tmp/",
    ):
        assert pattern in gitignore
