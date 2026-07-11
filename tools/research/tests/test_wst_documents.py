from __future__ import annotations

from pathlib import Path

from tools.research.wst.document_extractors import extract_document, write_document_extraction


def test_html_extractor_keeps_visible_text_and_drops_script(tmp_path: Path) -> None:
    source = tmp_path / "lesson.html"
    source.write_text(
        "<h1>Offer</h1><script>secret()</script><p>Visible text</p>", encoding="utf-8"
    )

    result = extract_document(source, 44)

    assert result["extraction_method"] == "html_visible_text"
    assert result["blocks"][0]["text"] == "Offer\nVisible text"
    assert result["blocks"][0]["source_ref"] == "wst://post/44/file/lesson.html"


def test_plain_text_and_unknown_document_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "lesson.txt"
    source.write_text("Only source text", encoding="utf-8")
    unknown = tmp_path / "archive.bin"
    unknown.write_bytes(b"x")

    text_result = extract_document(source, 45)
    unknown_result = extract_document(unknown, 45)

    assert text_result["blocks"][0]["text"] == "Only source text"
    assert unknown_result["blocks"] == []
    assert "Unsupported document format" in unknown_result["errors"][0]


def test_document_extraction_writes_json_and_markdown(tmp_path: Path) -> None:
    source = tmp_path / "lesson.txt"
    source.write_text("Evidence", encoding="utf-8")
    result = extract_document(source, 46)

    json_path, markdown_path = write_document_extraction(result, tmp_path / "out")

    assert json_path.exists() and markdown_path.exists()
    assert "wst://post/46/file/lesson.txt" in markdown_path.read_text(encoding="utf-8")
