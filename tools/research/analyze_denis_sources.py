from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

try:
    from .sanitize import PROJECT_ROOT, ensure_research_path, ensure_research_tree
except ImportError:  # pragma: no cover - direct script execution
    from sanitize import PROJECT_ROOT, ensure_research_path, ensure_research_tree

WORD_RE = re.compile(r"(?u)\b[^\W\d_][^\W_]{2,}\b")
SANITIZED_FILES = (
    "telegram_posts.md",
    "telegram_comments.md",
    "youtube_videos.md",
    "youtube_comments.md",
)


def analyze_sanitized(project_root: Path = PROJECT_ROOT, top: int = 50) -> Path:
    paths = ensure_research_tree(project_root)
    counter: Counter[str] = Counter()
    loaded_files: list[str] = []

    for name in SANITIZED_FILES:
        source = ensure_research_path(paths["sanitized"] / name, project_root)
        if not source.exists():
            continue
        text = source.read_text(encoding="utf-8")
        counter.update(word.lower() for word in WORD_RE.findall(text))
        loaded_files.append(name)

    target = ensure_research_path(paths["summaries"] / "local_frequency_summary.md", project_root)
    lines = [
        "# Local frequency summary",
        "",
        "Input files:",
        *(f"- {name}" for name in loaded_files),
        "",
        f"Top terms: {top}",
        "",
    ]
    for word, count in counter.most_common(top):
        lines.append(f"- {word}: {count}")

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze sanitized local Denis channel research files."
    )
    parser.add_argument("--top", type=int, default=50, help="Number of terms to include.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    target = analyze_sanitized(args.project_root, max(1, args.top))
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
