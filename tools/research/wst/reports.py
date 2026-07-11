from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_report(path: Path, title: str, sections: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", "", f"Generated: {datetime.now(UTC).isoformat()}", ""]
    for name, value in sections.items():
        lines.extend([f"## {name}", ""])
        if isinstance(value, dict):
            lines.extend(f"- {key}: {item}" for key, item in value.items())
        elif isinstance(value, (list, tuple)):
            lines.extend(f"- {item}" for item in value) if value else lines.append("- None")
        else:
            lines.append(str(value))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
