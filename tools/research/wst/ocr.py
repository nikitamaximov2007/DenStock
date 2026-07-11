from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def available_ocr_languages() -> set[str]:
    try:
        import pytesseract
    except ImportError:
        return set()
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception:  # noqa: BLE001 - local binary may be absent or misconfigured.
        return set()


def run_ocr(image_path: Path, languages: str = "rus+eng") -> dict[str, Any]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("pytesseract and Pillow are required for OCR.") from exc
    with Image.open(image_path) as image:
        data = pytesseract.image_to_data(image, lang=languages, output_type=pytesseract.Output.DICT)
    words = []
    confidences = []
    for text, confidence in zip(data.get("text", []), data.get("conf", []), strict=False):
        if not str(text).strip():
            continue
        value = float(confidence) if str(confidence) not in {"", "-1"} else 0.0
        words.append(str(text).strip())
        confidences.append(value)
    average = sum(confidences) / len(confidences) if confidences else 0.0
    return {
        "raw_text": " ".join(words),
        "normalized_text": " ".join(words),
        "confidence": average,
        "low_confidence": average < 60,
    }


def write_ocr(post_id: int, frames: list[dict[str, Any]], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {"post_id": post_id, "frames": frames}
    json_path = output_dir / f"{post_id}.json"
    markdown_path = output_dir / f"{post_id}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# OCR кадров WST", "", f"Telegram post: {post_id}", ""]
    for frame in frames:
        lines.extend(
            [
                f"## {frame['timestamp']}",
                "",
                frame.get("normalized_text", ""),
                "",
                f"Confidence: {frame.get('confidence', 0):.1f}",
                frame["source_ref"],
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path
