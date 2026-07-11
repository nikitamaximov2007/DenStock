from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # Direct `python tools/research/...` execution.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.research.wst.config import load_settings, wst_paths
from tools.research.wst.document_extractors import extract_document, write_document_extraction
from tools.research.wst.downloader import download_media
from tools.research.wst.models import message_record
from tools.research.wst.navigation import build_navigation_graph
from tools.research.wst.ocr import run_ocr, write_ocr
from tools.research.wst.reports import write_report
from tools.research.wst.state import WSTState
from tools.research.wst.telegram_source import (
    WSTAccessError,
    WSTTelegramSource,
    doctor_channel_access,
)
from tools.research.wst.video_frames import (
    extract_keyframe,
    periodic_timestamps,
    retain_distinct_frames,
)
from tools.research.wst.video_transcriber import (
    extract_mono_audio,
    probe_media,
    transcribe_audio,
    write_transcript,
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _write_jsonl_by_key(path: Path, records: list[dict[str, Any]], key: str) -> None:
    existing = {str(item[key]): item for item in _read_jsonl(path) if key in item}
    existing.update({str(item[key]): item for item in records if key in item})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in sorted(existing.values(), key=lambda row: int(row[key]))
        ),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _within_period(record: dict[str, Any], since: str | None, until: str | None) -> bool:
    date = str(record.get("date") or "")
    return (not since or date >= since) and (not until or date <= until)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local read-only WST Telegram collector.")
    parser.add_argument("--output-root", help="Must remain under research_inputs/wst.")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "collect-navigation", "collect-all"):
        item = subparsers.add_parser(name)
        item.add_argument("--channel-id", type=int)
        item.add_argument("--navigation-message-id", type=int)
        item.add_argument("--limit", type=int)
        item.add_argument("--since")
        item.add_argument("--until")
        item.add_argument("--message-id", type=int)
        item.add_argument("--download-media", action="store_true")
        item.add_argument("--skip-existing", action="store_true")
        item.add_argument("--retry-failed", action="store_true")
        item.add_argument("--dry-run", action="store_true")
    process = subparsers.add_parser("process")
    process.add_argument("--whisper-model")
    process.add_argument("--device", default="auto")
    process.add_argument("--compute-type", default="auto")
    process.add_argument("--max-video-duration", type=int)
    process.add_argument("--skip-existing", action="store_true")
    process.add_argument("--retry-failed", action="store_true")
    process.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("doctor")
    return parser


async def collect(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(
        output_root=args.output_root,
        channel_id=args.channel_id,
        navigation_message_id=args.navigation_message_id,
    )
    if args.dry_run:
        return {"mode": args.command, "dry_run": True, "output_root": str(settings.output_root)}
    paths = wst_paths(settings)
    with WSTState(paths["state"] / "wst_state.sqlite3") as state:
        async with WSTTelegramSource(settings) as source:
            await source.find_channel()
            root = await source.get_message(settings.navigation_message_id)
            if root is None:
                raise WSTAccessError("WST navigation message is unavailable.")
            graph = await build_navigation_graph(
                settings.navigation_message_id, source.get_message, settings.channel_id
            )
            _write_json(paths["raw"] / "navigation_graph.json", graph)
            node_paths = {item["message_id"]: item for item in graph["nodes"]}
            messages: dict[int, tuple[Any, str, list[int]]] = {}
            if args.command == "collect-all":
                async for message in source.iter_messages(limit=args.limit):
                    node = node_paths.get(getattr(message, "id", None))
                    discovery = "both" if node else "full_channel_scan"
                    navigation_path = node["navigation_path"] if node else []
                    messages[int(message.id)] = (message, discovery, navigation_path)
            else:
                for node in graph["nodes"]:
                    if node["status"] != "available":
                        continue
                    message = await source.get_message(node["message_id"])
                    if message is not None:
                        messages[int(message.id)] = (
                            message,
                            node["discovery_source"],
                            node["navigation_path"],
                        )
            if args.message_id:
                messages = {key: value for key, value in messages.items() if key == args.message_id}
            records, manifest = [], []
            for message, discovery, navigation_path in messages.values():
                record = message_record(
                    message,
                    channel_id=settings.channel_id,
                    discovery_source=discovery,
                    navigation_path=navigation_path,
                )
                if not _within_period(record, args.since, args.until):
                    continue
                state.record_post(record)
                records.append(record)
                if not record["has_media"]:
                    continue
                media = {
                    "message_id": record["message_id"],
                    **{
                        key: record.get(key)
                        for key in (
                            "media_type",
                            "file_name",
                            "mime_type",
                            "size_bytes",
                            "duration_seconds",
                        )
                    },
                }
                saved = state.media_record(record["message_id"])
                if args.download_media:
                    outcome = await download_media(
                        source.client,
                        message,
                        media,
                        paths,
                        existing_sha256=saved.get("sha256") if saved else None,
                    )
                    media.update(outcome)
                    state.update_media(
                        record["message_id"],
                        local_path=outcome.get("path"),
                        sha256=outcome.get("sha256"),
                        download_status=outcome["status"],
                        error_message=outcome.get("error"),
                        retry_count=(saved or {}).get("retry_count", 0)
                        + (outcome["status"] == "failed"),
                    )
                manifest.append(media)
            _write_jsonl_by_key(paths["raw"] / "posts.jsonl", records, "message_id")
            _write_jsonl_by_key(paths["raw"] / "media_manifest.jsonl", manifest, "message_id")
            albums = {}
            for record in records:
                if record.get("grouped_id"):
                    albums.setdefault(str(record["grouped_id"]), []).append(record["message_id"])
            _write_json(paths["raw"] / "album_manifest.json", {"albums": albums})
            summary = {
                "mode": args.command,
                "posts": len(records),
                "media": len(manifest),
                "navigation_posts": len(node_paths),
                "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in manifest),
                "total_duration_seconds": sum(
                    float(item.get("duration_seconds") or 0) for item in manifest
                ),
                "collected_at": datetime.now(UTC).isoformat(),
            }
            _write_json(paths["raw"] / "collection_manifest.json", summary)
            write_report(
                paths["reports"]
                / (
                    "inventory_report.md" if args.command == "inventory" else "collection_report.md"
                ),
                "WST collection",
                summary,
            )
            return summary


def process(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(output_root=args.output_root)
    paths = wst_paths(settings)
    media = _read_jsonl(paths["raw"] / "media_manifest.jsonl")
    if args.dry_run:
        return {"mode": "process", "dry_run": True, "media": len(media)}
    counts: Counter[str] = Counter()
    errors: list[str] = []
    for item in media:
        source_path = Path(item.get("path") or "")
        if not source_path.exists():
            counts["missing"] += 1
            continue
        post_id = int(item["message_id"])
        try:
            if source_path.suffix.lower() in {
                ".pdf",
                ".pptx",
                ".docx",
                ".xlsx",
                ".html",
                ".htm",
                ".txt",
                ".md",
            }:
                result = extract_document(source_path, post_id)
                write_document_extraction(result, paths["extracted_documents"])
                counts["documents"] += 1
            elif item.get("media_type") in {"video", "audio"}:
                metadata = probe_media(source_path)
                if not metadata["audio_streams"]:
                    counts["without_audio"] += 1
                    continue
                audio_path = extract_mono_audio(source_path, paths["state"] / "tmp")
                segments = transcribe_audio(
                    audio_path,
                    model_name=args.whisper_model or settings.whisper_model,
                    device=args.device,
                    compute_type=args.compute_type,
                )
                write_transcript(
                    post_id, metadata["duration_seconds"], segments, paths["transcripts"]
                )
                audio_path.unlink(missing_ok=True)
                counts["transcripts"] += 1
                if item.get("media_type") == "video":
                    _process_video_frames(
                        post_id, source_path, metadata, paths, settings.ocr_languages
                    )
                    counts["video_evidence"] += 1
            else:
                counts["unsupported"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad local asset must not stop a run.
            errors.append(f"post {post_id}: {exc.__class__.__name__}: {exc}")
    write_report(
        paths["reports"] / "extraction_report.md",
        "WST extraction",
        {"counts": dict(counts), "errors": errors},
    )
    return {"mode": "process", **dict(counts), "errors": len(errors)}


def _process_video_frames(
    post_id: int,
    source_path: Path,
    metadata: dict[str, Any],
    paths: dict[str, Path],
    languages: str,
) -> None:
    frame_dir = paths["keyframes"] / str(post_id)
    candidates = [
        (timestamp, extract_keyframe(source_path, timestamp, frame_dir))
        for timestamp in periodic_timestamps(metadata["duration_seconds"])
    ]
    frames = []
    for item in retain_distinct_frames(candidates):
        ocr = run_ocr(Path(item["path"]), languages)
        frames.append(
            {
                **item,
                **ocr,
                "source_ref": f"wst://post/{post_id}/frame?t={item['timestamp']}",
            }
        )
    write_ocr(post_id, frames, paths["ocr"])
    evidence_path = paths["extracted"] / "evidence"
    evidence_path.mkdir(parents=True, exist_ok=True)
    (evidence_path / f"{post_id}.md").write_text(
        "# WST video evidence\n\n"
        f"Telegram post: {post_id}\n\n"
        f"Transcript: ../transcripts/{post_id}.md\n"
        f"OCR: ../ocr/{post_id}.md\n",
        encoding="utf-8",
    )


async def doctor(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(output_root=args.output_root)
    paths = wst_paths(settings)
    report = {
        "telegram_credentials_present": settings.has_telegram_credentials,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "free_disk_bytes": shutil.disk_usage(settings.output_root).free,
        "output_directories": all(path.exists() for path in paths.values()),
    }
    try:
        from tools.research.wst.ocr import available_ocr_languages

        languages = available_ocr_languages()
        report["ocr_languages"] = sorted(languages)
        report["ocr_rus_eng"] = {"rus", "eng"}.issubset(languages)
    except Exception:  # noqa: BLE001
        report["ocr_rus_eng"] = False
    try:
        import faster_whisper  # noqa: F401

        report["faster_whisper"] = True
    except ImportError:
        report["faster_whisper"] = False
    if settings.has_telegram_credentials:
        report.update(await doctor_channel_access(settings))
    write_report(paths["reports"] / "inventory_report.md", "WST doctor", report)
    return report


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "doctor":
            result = asyncio.run(doctor(args))
        elif args.command == "process":
            result = process(args)
        else:
            result = asyncio.run(collect(args))
    except (WSTAccessError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 2
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
