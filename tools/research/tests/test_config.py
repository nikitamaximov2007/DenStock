from __future__ import annotations

import argparse
from pathlib import Path

from tools.research.collect_denis_sources import (
    DEFAULT_LIMIT,
    MAX_COMMENT_LIMIT,
    build_runtime_config,
    safe_int,
)


def test_safe_int_uses_defaults_and_caps_values() -> None:
    assert safe_int("bad", 10) == 10
    assert safe_int("-1", 10) == 10
    assert safe_int("999", 10, maximum=50) == 50
    assert safe_int("25", 10, maximum=50) == 25


def test_config_reads_limits_safely(tmp_path: Path) -> None:
    args = argparse.Namespace(
        telegram=False,
        youtube=False,
        no_comments=False,
        posts_only=False,
        limit=None,
        comment_limit=None,
    )
    config = build_runtime_config(
        args,
        environ={
            "RESEARCH_LIMIT": "-10",
            "RESEARCH_COMMENT_LIMIT": "999999",
            "TG_API_ID": "12345",
            "TG_API_HASH": "hash",
            "YT_API_KEY": "key",
        },
        project_root=tmp_path,
    )

    assert config.telegram is True
    assert config.youtube is True
    assert config.include_comments is True
    assert config.limit == DEFAULT_LIMIT
    assert config.comment_limit == MAX_COMMENT_LIMIT
    assert config.has_telegram_credentials is True
    assert config.has_youtube_credentials is True
