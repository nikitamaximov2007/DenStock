from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # Direct `python tools/research/...` execution.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.research.wst.config import load_settings, wst_paths
from tools.research.wst.corpus import (
    build_fts,
    build_packs,
    document_chunks,
    ocr_chunks,
    post_chunks,
    read_jsonl,
    search_fts,
    transcript_chunks,
    validate_chunks,
    write_jsonl,
)
from tools.research.wst.reports import write_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a local WST evidence corpus.")
    parser.add_argument("--output-root")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--pack-max-mb", type=float, default=8)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--strict", action="store_true")
    search = subparsers.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    return parser


def _load_documents(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")]


def build(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(output_root=args.output_root)
    paths = wst_paths(settings)
    posts = read_jsonl(paths["raw"] / "posts.jsonl")
    chunks = post_chunks(posts)
    for transcript_path in paths["transcripts"].glob("*.json"):
        chunks.extend(transcript_chunks(json.loads(transcript_path.read_text(encoding="utf-8"))))
    for document in _load_documents(paths["extracted_documents"]):
        chunks.extend(document_chunks(document))
    for ocr_path in paths["ocr"].glob("*.json"):
        chunks.extend(ocr_chunks(json.loads(ocr_path.read_text(encoding="utf-8"))))
    write_jsonl(paths["normalized"] / "chunks.jsonl", chunks)
    materials = [
        {
            "message_id": post["message_id"],
            "canonical_url": post["canonical_url"],
            "media_type": post.get("media_type"),
            "processing_status": post.get("processing_status"),
        }
        for post in posts
    ]
    write_jsonl(paths["normalized"] / "materials.jsonl", materials)
    source_index = [
        {
            "chunk_id": item["chunk_id"],
            "source_ref": item["source_ref"],
            "message_id": item["message_id"],
        }
        for item in chunks
    ]
    write_jsonl(paths["normalized"] / "source_index.jsonl", source_index)
    build_fts(paths["index"] / "wst_index.sqlite3", chunks)
    _write_ai_corpus(paths, posts, chunks, int(args.pack_max_mb * 1024 * 1024))
    manifest = {
        "posts": len(posts),
        "chunks": len(chunks),
        "packs": sorted(path.name for path in (paths["ai_corpus"] / "packs").glob("*.md")),
    }
    (paths["ai_corpus"] / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _write_ai_corpus(
    paths: dict[str, Path],
    posts: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    pack_size: int,
) -> None:
    corpus = paths["ai_corpus"]
    for name in ("posts", "videos", "documents", "branches", "packs", "reports"):
        (corpus / name).mkdir(parents=True, exist_ok=True)
    for post in posts:
        post_chunks_for_id = [item for item in chunks if item["message_id"] == post["message_id"]]
        content = "\n\n".join(
            f"{item['source_ref']}\n\n{item['text']}" for item in post_chunks_for_id
        )
        (corpus / "posts" / f"post-{post['message_id']}.md").write_text(
            f"# Post {post['message_id']}\n\n{content}\n", encoding="utf-8"
        )
    packs = build_packs(chunks, corpus / "packs", pack_size)
    index = ["# WST corpus index", ""] + [
        f"- post-{post['message_id']}: {post['canonical_url']}" for post in posts
    ]
    (corpus / "00-corpus-index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    (corpus / "01-navigation-map.md").write_text(
        "# Navigation map\n\nSee raw/navigation_graph.json.\n", encoding="utf-8"
    )
    (corpus / "reports" / "packs.md").write_text(
        "\n".join(f"- {name}" for name in packs) + "\n", encoding="utf-8"
    )


def validate(args: argparse.Namespace) -> tuple[list[str], Path]:
    settings = load_settings(output_root=args.output_root)
    paths = wst_paths(settings)
    chunks = read_jsonl(paths["normalized"] / "chunks.jsonl")
    errors = validate_chunks(chunks, paths["media"])
    graph_path = paths["raw"] / "navigation_graph.json"
    if graph_path.exists():
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        node_ids = {item["message_id"] for item in graph.get("nodes", [])}
        for edge in graph.get("edges", []):
            child = edge.get("child_message_id")
            if child is not None and child not in node_ids:
                errors.append(f"Broken navigation edge to {child}")
    if not chunks:
        errors.append("Corpus has no chunks")
    report = write_report(
        paths["reports"] / "validation_report.md", "WST validation", {"errors": errors}
    )
    return errors, report


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "build":
        print(json.dumps(build(args), ensure_ascii=False))
        return 0
    if args.command == "validate":
        errors, report = validate(args)
        print(f"report: {report}")
        for error in errors:
            print(f"error: {error}")
        return 1 if errors and args.strict else 0
    settings = load_settings(output_root=args.output_root)
    paths = wst_paths(settings)
    for result in search_fts(paths["index"] / "wst_index.sqlite3", args.query, args.limit):
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
