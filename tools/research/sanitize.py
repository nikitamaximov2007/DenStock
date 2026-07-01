from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_INPUTS = "research_inputs"
CHANNEL_ROOT = Path(RESEARCH_INPUTS) / "denis_channels"

TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram_posts": ("text",),
    "telegram_comments": ("text",),
    "youtube_videos": ("title", "description"),
    "youtube_comments": ("text",),
}

ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram_posts": (
        "source",
        "record_type",
        "post_id",
        "url",
        "date",
        "text",
        "has_photo",
        "has_video",
        "views",
        "forwards",
        "grouped_id",
    ),
    "telegram_comments": (
        "source",
        "record_type",
        "parent_post_id",
        "parent_url",
        "date",
        "text",
        "reactions_count",
    ),
    "youtube_videos": (
        "source",
        "record_type",
        "video_id",
        "url",
        "date",
        "title",
        "description",
        "view_count",
        "like_count",
        "comment_count",
    ),
    "youtube_comments": (
        "source",
        "record_type",
        "video_id",
        "video_url",
        "date",
        "text",
        "like_count",
        "is_reply",
    ),
}

RAW_TO_MARKDOWN: dict[str, tuple[str, str, str]] = {
    "telegram_posts.jsonl": (
        "telegram_posts",
        "telegram_posts.md",
        "Telegram posts",
    ),
    "telegram_comments.jsonl": (
        "telegram_comments",
        "telegram_comments.md",
        "Telegram comments",
    ),
    "youtube_videos.jsonl": (
        "youtube_videos",
        "youtube_videos.md",
        "YouTube videos",
    ),
    "youtube_comments.jsonl": (
        "youtube_comments",
        "youtube_comments.md",
        "YouTube comments",
    ),
}

EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)(?!\w)")
USERNAME_RE = re.compile(r"(?<![\w/])@[a-zA-Z0-9_]{5,32}\b")
LINK_RE = re.compile(
    r"(?i)\b(?:"
    r"https?://|www\.|t\.me/|telegram\.me/|tg://|vk\.com/|wa\.me/|"
    r"instagram\.com/|facebook\.com/|ok\.ru/|mailto:"
    r")[^\s<>()\[\]{}\"']+"
)
DOMAIN_LINK_RE = re.compile(
    r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)+/(?:[^\s<>()\[\]{}\"']+)"
)
ADDRESS_RE = re.compile(
    r"(?i)\b(?:"
    r"ул\.?|улица|просп\.?|пр-т|проспект|пер\.?|переулок|шоссе|"
    r"д\.|дом|кв\.|квартира"
    r")\s+[^,\n.;]{1,80}"
)


def research_root(project_root: Path | None = None) -> Path:
    root = PROJECT_ROOT if project_root is None else Path(project_root)
    return (root / CHANNEL_ROOT).resolve()


def ensure_research_path(path: Path | str, project_root: Path | None = None) -> Path:
    root = research_root(project_root)
    candidate = Path(path)
    if not candidate.is_absolute():
        base = PROJECT_ROOT if project_root is None else Path(project_root)
        candidate = base / candidate
    resolved = candidate.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        msg = f"Refusing to write outside {CHANNEL_ROOT}: {resolved}"
        raise ValueError(msg)
    return resolved


def ensure_research_tree(project_root: Path | None = None) -> dict[str, Path]:
    root = research_root(project_root)
    paths = {
        "root": root,
        "raw": root / "raw",
        "sanitized": root / "sanitized",
        "summaries": root / "summaries",
    }
    for path in paths.values():
        ensure_research_path(path, project_root).mkdir(parents=True, exist_ok=True)
    return paths


def sanitize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = EMAIL_RE.sub("[email redacted]", text)
    text = PHONE_RE.sub(_redact_phone, text)
    text = LINK_RE.sub("[link redacted]", text)
    text = DOMAIN_LINK_RE.sub("[link redacted]", text)
    text = USERNAME_RE.sub("[username redacted]", text)
    return ADDRESS_RE.sub("[address redacted]", text)


def _redact_phone(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) < 10:
        return match.group(0)
    return "[phone redacted]"


def sanitize_record(record: Mapping[str, Any], record_type: str) -> dict[str, Any]:
    if record_type not in ALLOWED_FIELDS:
        msg = f"Unknown record type: {record_type}"
        raise ValueError(msg)

    allowed = ALLOWED_FIELDS[record_type]
    text_fields = set(TEXT_FIELDS.get(record_type, ()))
    cleaned: dict[str, Any] = {"record_type": record_type}

    for field in allowed:
        if field == "record_type":
            continue
        if field not in record:
            continue
        value = record[field]
        if value is None:
            continue
        cleaned[field] = sanitize_text(value) if field in text_fields else value

    return cleaned


def write_jsonl(
    records: Iterable[Mapping[str, Any]],
    path: Path | str,
    record_type: str,
    project_root: Path | None = None,
) -> int:
    target = ensure_research_path(path, project_root)
    target.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with target.open("w", encoding="utf-8") as handle:
        for record in records:
            cleaned = sanitize_record(record, record_type)
            handle.write(json.dumps(cleaned, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            count += 1
    return count


def read_jsonl(path: Path | str, project_root: Path | None = None) -> list[dict[str, Any]]:
    source = ensure_research_path(path, project_root)
    records: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                records.append(json.loads(stripped))
    return records


def format_markdown(
    records: Sequence[Mapping[str, Any]],
    record_type: str,
    title: str | None = None,
) -> str:
    heading = title or record_type.replace("_", " ").title()
    cleaned = [sanitize_record(record, record_type) for record in records]
    lines = [f"# {heading}", "", f"Records: {len(cleaned)}", ""]

    if record_type.endswith("comments"):
        lines.extend(_format_comment_markdown(cleaned))
    else:
        lines.extend(_format_content_markdown(cleaned, record_type))

    return "\n".join(lines).rstrip() + "\n"


def _format_content_markdown(records: Sequence[Mapping[str, Any]], record_type: str) -> list[str]:
    lines: list[str] = []
    for index, record in enumerate(records, start=1):
        identifier = record.get("post_id") or record.get("video_id") or index
        lines.append(f"## {identifier}")
        _append_optional_line(lines, "Date", record.get("date"))
        _append_optional_line(lines, "Link", record.get("url"))

        if record_type == "telegram_posts":
            _append_optional_line(lines, "Views", record.get("views"))
            _append_optional_line(lines, "Forwards", record.get("forwards"))
            lines.append(f"- Has photo: {bool(record.get('has_photo'))}")
            lines.append(f"- Has video: {bool(record.get('has_video'))}")
            body = str(record.get("text", "")).strip()
        else:
            _append_optional_line(lines, "Views", record.get("view_count"))
            _append_optional_line(lines, "Likes", record.get("like_count"))
            _append_optional_line(lines, "Comments", record.get("comment_count"))
            title = str(record.get("title", "")).strip()
            description = str(record.get("description", "")).strip()
            body = "\n\n".join(part for part in (title, description) if part)

        lines.append("")
        lines.append(body or "_No text._")
        lines.append("")
    return lines


def _format_comment_markdown(records: Sequence[Mapping[str, Any]]) -> list[str]:
    texts = [str(record.get("text", "")).strip() for record in records]
    counts = Counter(text for text in texts if text)
    lines = [f"Unique sanitized texts: {len(counts)}", ""]

    for index, (text, count) in enumerate(counts.most_common(), start=1):
        examples = [record for record in records if str(record.get("text", "")).strip() == text]
        first = examples[0] if examples else {}
        lines.append(f"## Comment topic {index}")
        lines.append(f"- Count: {count}")
        _append_optional_line(lines, "Date example", first.get("date"))
        _append_optional_line(lines, "Parent post", first.get("parent_url"))
        _append_optional_line(lines, "Video", first.get("video_url"))
        lines.append("")
        lines.append(text)
        lines.append("")
    return lines


def _append_optional_line(lines: list[str], label: str, value: Any) -> None:
    if value not in (None, ""):
        lines.append(f"- {label}: {value}")


def write_markdown(
    records: Sequence[Mapping[str, Any]],
    path: Path | str,
    record_type: str,
    title: str | None = None,
    project_root: Path | None = None,
) -> int:
    target = ensure_research_path(path, project_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_markdown(records, record_type, title), encoding="utf-8")
    return len(records)


def sanitize_research_inputs(project_root: Path | None = None) -> dict[str, int]:
    paths = ensure_research_tree(project_root)
    stats: dict[str, int] = {}

    for raw_name, (record_type, markdown_name, title) in RAW_TO_MARKDOWN.items():
        raw_path = paths["raw"] / raw_name
        markdown_path = paths["sanitized"] / markdown_name
        if not raw_path.exists():
            stats[markdown_name] = 0
            write_markdown([], markdown_path, record_type, title, project_root)
            continue

        records = read_jsonl(raw_path, project_root)
        stats[markdown_name] = write_markdown(
            records,
            markdown_path,
            record_type,
            title,
            project_root,
        )

    return stats


def write_collection_report(
    report: Mapping[str, Any],
    path: Path | str,
    project_root: Path | None = None,
) -> Path:
    target = ensure_research_path(path, project_root)
    target.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Collection report",
        "",
        f"Generated at: {datetime.now(UTC).isoformat()}",
        "",
        "## Counts",
    ]
    for key, value in sorted(report.get("counts", {}).items()):
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Limitations"])
    limitations = list(report.get("limitations", []))
    if limitations:
        lines.extend(f"- {item}" for item in limitations)
    else:
        lines.append("- None recorded")

    lines.extend(["", "## Notes"])
    for note in report.get("notes", []):
        lines.append(f"- {note}")

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sanitize local Denis channel research files.")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Project root. Outputs remain under research_inputs/denis_channels.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    stats = sanitize_research_inputs(args.project_root)
    for name, count in sorted(stats.items()):
        print(f"{name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
