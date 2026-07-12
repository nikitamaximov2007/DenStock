from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


def available_ocr_languages() -> set[str]:
    """Return only locally installed Tesseract language packs."""
    try:
        import pytesseract
    except ImportError:
        return set()
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception:  # noqa: BLE001 - local binary may be absent or misconfigured.
        return set()


def available_ocr_backends() -> dict[str, bool]:
    tesseract_ready = {"rus", "eng"}.issubset(available_ocr_languages())
    try:
        import easyocr  # noqa: F401
    except ImportError:
        easyocr_ready = False
    else:
        easyocr_ready = True
    return {"tesseract": tesseract_ready, "easyocr": easyocr_ready}


def active_ocr_backend() -> str | None:
    backends = available_ocr_backends()
    if backends["tesseract"]:
        return "tesseract"
    if backends["easyocr"]:
        return "easyocr"
    return None


def run_ocr(image_path: Path, languages: str = "rus+eng", backend: str = "auto") -> dict[str, Any]:
    """Run OCR strictly in-process using local Tesseract or EasyOCR models."""
    selected = active_ocr_backend() if backend == "auto" else backend
    if selected == "tesseract":
        return _run_tesseract(image_path, languages)
    if selected == "easyocr":
        return _run_easyocr(image_path)
    raise RuntimeError("No local OCR backend is ready; run bootstrap-media --install.")


def _run_tesseract(image_path: Path, languages: str) -> dict[str, Any]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("pytesseract and Pillow are required for Tesseract OCR.") from exc
    with Image.open(image_path) as image:
        data = pytesseract.image_to_data(image, lang=languages, output_type=pytesseract.Output.DICT)
    words, confidences = _tesseract_words(data)
    return _result(words, confidences, "tesseract")


@lru_cache(maxsize=1)
def _easyocr_reader() -> Any:
    try:
        import easyocr
    except ImportError as exc:
        raise RuntimeError("EasyOCR is not installed; run bootstrap-media --install.") from exc
    cache = Path(__file__).resolve().parents[1] / ".cache" / "models" / "easyocr"
    cache.mkdir(parents=True, exist_ok=True)
    return easyocr.Reader(
        ["ru", "en"], gpu=False, model_storage_directory=str(cache), verbose=False
    )


def _run_easyocr(image_path: Path) -> dict[str, Any]:
    results = _easyocr_reader().readtext(str(image_path), detail=1, paragraph=False)
    words = [str(item[1]).strip() for item in results if str(item[1]).strip()]
    confidences = [float(item[2]) * 100 for item in results if str(item[1]).strip()]
    return _result(words, confidences, "easyocr")


def _tesseract_words(data: dict[str, Any]) -> tuple[list[str], list[float]]:
    words, confidences = [], []
    for text, confidence in zip(data.get("text", []), data.get("conf", []), strict=False):
        if not str(text).strip():
            continue
        words.append(str(text).strip())
        confidences.append(float(confidence) if str(confidence) not in {"", "-1"} else 0.0)
    return words, confidences


def _result(words: list[str], confidences: list[float], backend: str) -> dict[str, Any]:
    average = sum(confidences) / len(confidences) if confidences else 0.0
    return {
        "raw_text": " ".join(words),
        "normalized_text": " ".join(words),
        "confidence": average,
        "low_confidence": average < 60,
        "backend": backend,
        "status": "complete" if words else "no_text_detected",
    }


def write_ocr(post_id: int, frames: list[dict[str, Any]], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {"post_id": post_id, "frames": frames}
    json_path = output_dir / f"{post_id}.json"
    markdown_path = output_dir / f"{post_id}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# OCR frames WST", "", f"Telegram post: {post_id}", ""]
    for frame in frames:
        lines.extend(
            [
                f"## {frame['timestamp']}",
                "",
                frame.get("normalized_text", ""),
                "",
                f"Confidence: {frame.get('confidence', 0):.1f}",
                f"Backend: {frame.get('backend', 'unknown')}",
                frame["source_ref"],
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path
