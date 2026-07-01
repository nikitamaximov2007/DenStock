from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .sanitize import (
        CHANNEL_ROOT,
        PROJECT_ROOT,
        ensure_research_tree,
        sanitize_research_inputs,
        write_collection_report,
    )
    from .telegram_collector import default_session_path, run_telegram_collection
    from .youtube_collector import collect_youtube
except ImportError:  # pragma: no cover - direct script execution
    from sanitize import (
        CHANNEL_ROOT,
        PROJECT_ROOT,
        ensure_research_tree,
        sanitize_research_inputs,
        write_collection_report,
    )
    from telegram_collector import default_session_path, run_telegram_collection
    from youtube_collector import collect_youtube

DEFAULT_LIMIT = 50
DEFAULT_COMMENT_LIMIT = 100
MAX_LIMIT = 500
MAX_COMMENT_LIMIT = 1000


@dataclass(frozen=True)
class RuntimeConfig:
    telegram: bool
    youtube: bool
    include_comments: bool
    limit: int
    comment_limit: int
    tg_api_id: str
    tg_api_hash: str
    yt_api_key: str
    project_root: Path

    @property
    def has_telegram_credentials(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash)

    @property
    def has_youtube_credentials(self) -> bool:
        return bool(self.yt_api_key)


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def safe_int(
    value: Any,
    default: int,
    minimum: int = 1,
    maximum: int = MAX_LIMIT,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return default
    return min(parsed, maximum)


def build_runtime_config(
    args: argparse.Namespace,
    environ: Mapping[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> RuntimeConfig:
    file_values = load_env_file(project_root / ".env.research.local")
    env = {**file_values, **dict(os.environ if environ is None else environ)}

    telegram = bool(args.telegram)
    youtube = bool(args.youtube)
    if not telegram and not youtube:
        telegram = True
        youtube = True

    limit = args.limit
    if limit is None:
        limit = safe_int(env.get("RESEARCH_LIMIT"), DEFAULT_LIMIT, maximum=MAX_LIMIT)

    comment_limit = args.comment_limit
    if comment_limit is None:
        comment_limit = safe_int(
            env.get("RESEARCH_COMMENT_LIMIT"),
            DEFAULT_COMMENT_LIMIT,
            maximum=MAX_COMMENT_LIMIT,
        )

    return RuntimeConfig(
        telegram=telegram,
        youtube=youtube,
        include_comments=not (args.no_comments or args.posts_only),
        limit=safe_int(limit, DEFAULT_LIMIT, maximum=MAX_LIMIT),
        comment_limit=safe_int(
            comment_limit,
            DEFAULT_COMMENT_LIMIT,
            maximum=MAX_COMMENT_LIMIT,
        ),
        tg_api_id=env.get("TG_API_ID", ""),
        tg_api_hash=env.get("TG_API_HASH", ""),
        yt_api_key=env.get("YT_API_KEY", ""),
        project_root=project_root,
    )


def dry_run_summary(config: RuntimeConfig) -> str:
    lines = [
        "Dry run: no network calls and no files will be written.",
        f"Output root: {CHANNEL_ROOT}",
        f"Telegram enabled: {config.telegram}",
        f"Telegram credentials present: {config.has_telegram_credentials}",
        f"Telegram session path: {default_session_path()}",
        f"YouTube enabled: {config.youtube}",
        f"YouTube API key present: {config.has_youtube_credentials}",
        f"Limit: {config.limit}",
        f"Comment limit: {config.comment_limit}",
        f"Comments enabled: {config.include_comments}",
    ]
    return "\n".join(lines)


def collect_sources(config: RuntimeConfig) -> dict[str, Any]:
    paths = ensure_research_tree(config.project_root)
    report: dict[str, Any] = {
        "counts": {},
        "limitations": [],
        "notes": [
            "Local files are written only under research_inputs/denis_channels.",
            "Raw JSONL is already field-whitelisted and text-redacted before writing.",
        ],
    }

    if config.telegram:
        if config.has_telegram_credentials:
            telegram_result = run_telegram_collection(
                api_id=config.tg_api_id,
                api_hash=config.tg_api_hash,
                project_root=config.project_root,
                post_limit=config.limit,
                comment_limit=config.comment_limit,
                include_comments=config.include_comments,
            )
            _merge_report(report, telegram_result.as_report())
        else:
            report["limitations"].append("Telegram skipped: TG_API_ID/TG_API_HASH missing.")

    if config.youtube:
        if config.has_youtube_credentials:
            youtube_result = collect_youtube(
                api_key=config.yt_api_key,
                project_root=config.project_root,
                video_limit=config.limit,
                comment_limit=config.comment_limit,
                include_comments=config.include_comments,
            )
            _merge_report(report, youtube_result.as_report())
        else:
            report["limitations"].append("YouTube skipped: YT_API_KEY missing.")

    sanitized_counts = sanitize_research_inputs(config.project_root)
    for name, count in sanitized_counts.items():
        report["counts"][f"sanitized_{name}"] = count

    write_collection_report(
        report,
        paths["summaries"] / "collection_report.md",
        config.project_root,
    )
    return report


def sanitize_only(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    paths = ensure_research_tree(project_root)
    counts = sanitize_research_inputs(project_root)
    report = {
        "counts": {f"sanitized_{name}": count for name, count in counts.items()},
        "limitations": [],
        "notes": ["Sanitize-only run: no network calls were made."],
    }
    write_collection_report(report, paths["summaries"] / "collection_report.md", project_root)
    return report


def _merge_report(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.get("counts", {}).items():
        target["counts"][key] = target["counts"].get(key, 0) + value
    target["limitations"].extend(source.get("limitations", []))
    target["notes"].extend(source.get("notes", []))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only local collector for Denis public Telegram and YouTube channels."
    )
    parser.add_argument("--telegram", action="store_true", help="Collect Telegram channel posts.")
    parser.add_argument("--youtube", action="store_true", help="Collect YouTube channel videos.")
    parser.add_argument("--limit", type=int, help="Max posts/videos per source.")
    parser.add_argument("--comment-limit", type=int, help="Max comments per post/video.")
    parser.add_argument("--no-comments", action="store_true", help="Skip comments.")
    parser.add_argument("--posts-only", action="store_true", help="Collect posts/videos only.")
    parser.add_argument(
        "--sanitize-only",
        action="store_true",
        help="Only sanitize existing raw JSONL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show resolved config without network calls or file writes.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = build_runtime_config(args)

    if args.dry_run:
        print(dry_run_summary(config))
        return 0

    if args.sanitize_only:
        report = sanitize_only(config.project_root)
    else:
        report = collect_sources(config)

    for key, value in sorted(report["counts"].items()):
        print(f"{key}: {value}")
    for limitation in report["limitations"]:
        print(f"limitation: {limitation}")
    print(f"Report: {CHANNEL_ROOT / 'summaries' / 'collection_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
