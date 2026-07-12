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

from tools.research.wst.bootstrap import bootstrap_media, bootstrap_ocr
from tools.research.wst.config import load_settings, wst_paths
from tools.research.wst.document_extractors import extract_document, write_document_extraction
from tools.research.wst.downloader import download_media
from tools.research.wst.media_pipeline import process_media_record
from tools.research.wst.media_recovery import executable, executable_version
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
        item.add_argument("--download-timeout", type=int, default=300)
        item.add_argument("--download-chunk-size-mb", type=int, default=4)
        item.add_argument("--download-checkpoint-mb", type=int, default=16)
        item.add_argument("--max-download-attempts", type=int, default=3)
        item.add_argument("--resume-download", action=argparse.BooleanOptionalAction, default=True)
        item.add_argument("--restart-download", action="store_true")
        item.add_argument("--skip-existing", action="store_true")
        item.add_argument("--retry-failed", action="store_true")
        item.add_argument("--dry-run", action="store_true")
    process = subparsers.add_parser("process")
    process.add_argument("--whisper-model")
    process.add_argument("--device", default="auto")
    process.add_argument("--compute-type", default="auto")
    process.add_argument(
        "--transcription-backend", default="auto", choices=("auto", "faster-whisper", "whisper-cpp")
    )
    process.add_argument("--chunk-minutes", type=int, default=15)
    process.add_argument("--chunk-overlap-seconds", type=int, default=20)
    process.add_argument("--retry-failed-chunks", action="store_true")
    process.add_argument("--max-video-duration", type=int)
    process.add_argument("--skip-existing", action="store_true")
    process.add_argument("--retry-failed", action="store_true")
    process.add_argument("--message-id", type=int)
    process.add_argument("--dry-run", action="store_true")
    bootstrap = subparsers.add_parser("bootstrap-media")
    bootstrap.add_argument("--install", action="store_true")
    bootstrap.add_argument("--download-model", action="store_true")
    bootstrap.add_argument("--whisper-model", default="large-v3")
    bootstrap_ocr_parser = subparsers.add_parser("bootstrap-ocr")
    bootstrap_ocr_parser.add_argument("--install", action="store_true")
    bootstrap_ocr_parser.add_argument("--download-model", action="store_true")
    bootstrap_ocr_parser.add_argument(
        "--backend", choices=("auto", "easyocr", "tesseract"), default="auto"
    )
    retry = subparsers.add_parser("retry-media")
    retry.add_argument("--only-download-failed", action="store_true")
    retry.add_argument("--only-probe-failed", action="store_true")
    retry.add_argument("--only-audio-failed", action="store_true")
    retry.add_argument("--only-transcript-failed", action="store_true")
    retry.add_argument("--only-ocr-failed", action="store_true")
    retry.add_argument("--message-id", type=int)
    retry.add_argument("--max-attempts", type=int, default=3)
    retry.add_argument("--backend", default="auto")
    retry.add_argument("--force-repair", action="store_true")
    download_status = subparsers.add_parser("download-status")
    download_status.add_argument("--message-id", type=int, required=True)
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
            if args.message_id:
                message = await source.get_message(args.message_id)
                if message is not None:
                    messages[int(message.id)] = (message, "direct_message", [])
            elif args.command == "collect-all":
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
                        timeout_seconds=args.download_timeout,
                        retries=max(args.max_download_attempts - 1, 0),
                        state=state,
                        chunk_size=args.download_chunk_size_mb * 1024 * 1024,
                        checkpoint_bytes=args.download_checkpoint_mb * 1024 * 1024,
                        resume=args.resume_download,
                        restart=args.restart_download,
                    )
                    media.update(outcome)
                    state.update_media(
                        record["message_id"],
                        local_path=outcome.get("path"),
                        sha256=outcome.get("sha256"),
                        download_status=outcome["status"],
                        error_message=outcome.get("error"),
                        retry_count=(saved or {}).get("retry_count", 0)
                        + int(outcome["status"] == "retry_pending"),
                    )
                manifest.append(media)
            _write_jsonl_by_key(paths["raw"] / "posts.jsonl", records, "message_id")
            _write_jsonl_by_key(paths["raw"] / "media_manifest.jsonl", manifest, "message_id")
            albums = {}
            for record in records:
                if record.get("grouped_id"):
                    albums.setdefault(str(record["grouped_id"]), []).append(record["message_id"])
            _write_json(paths["raw"] / "album_manifest.json", {"albums": albums})
            write_report(
                paths["reports"] / "external_links.md",
                "WST external links",
                {"links": [edge.get("external_url", "") for edge in graph["external_links"]]},
            )
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
    if args.message_id:
        media = [item for item in media if item.get("message_id") == args.message_id]
    if args.dry_run:
        return {"mode": "process", "dry_run": True, "media": len(media)}
    counts: Counter[str] = Counter()
    errors: list[str] = []
    with WSTState(paths["state"] / "wst_state.sqlite3") as state:
        for item in media:
            source_path = Path(item.get("path") or "")
            post_id = int(item["message_id"])
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
                try:
                    result = extract_document(source_path, post_id)
                    write_document_extraction(result, paths["extracted_documents"])
                    counts["documents"] += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"post {post_id}: {exc.__class__.__name__}: {exc}")
                continue
            if item.get("media_type") not in {"video", "audio"}:
                counts["unsupported"] += 1
                continue
            result = process_media_record(
                item,
                paths,
                state,
                whisper_model=args.whisper_model or settings.whisper_model,
                device=args.device,
                compute_type=args.compute_type,
                ocr_languages=settings.ocr_languages,
                chunk_seconds=args.chunk_minutes * 60,
                overlap_seconds=args.chunk_overlap_seconds,
            )
            counts[result["status"]] += 1
        errors.extend(
            f"post {row['message_id']} {row['stage']}: {row['error_message']}"
            for row in state.retry_queue()
        )
    write_report(
        paths["reports"] / "extraction_report.md",
        "WST extraction",
        {"counts": dict(counts), "errors": errors},
    )
    return {"mode": "process", **dict(counts), "errors": len(errors)}


def retry_media(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(output_root=args.output_root)
    paths = wst_paths(settings)
    stage = next(
        (
            name
            for enabled, name in (
                (args.only_download_failed, "downloaded"),
                (args.only_probe_failed, "integrity_checked"),
                (args.only_audio_failed, "audio_extracted"),
                (args.only_transcript_failed, "transcript_created"),
                (args.only_ocr_failed, "ocr_created"),
            )
            if enabled
        ),
        None,
    )
    with WSTState(paths["state"] / "wst_state.sqlite3") as state:
        queue = [
            row
            for row in state.retry_queue(stage=stage, message_id=args.message_id)
            if row["attempts"] < args.max_attempts
        ]
        write_report(
            paths["reports"] / "media_retry_queue.md",
            "WST media retry queue",
            {
                "items": [
                    f"post {row['message_id']} {row['stage']}: {row['next_action']}"
                    for row in queue
                ]
            },
        )
    return {"retry_pending": len(queue), "stage": stage or "all"}


def download_status(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(output_root=args.output_root)
    paths = wst_paths(settings)
    with WSTState(paths["state"] / "wst_state.sqlite3") as state:
        record = state.download_record(args.message_id)
    if record is None:
        return {
            "message_id": args.message_id,
            "status": "pending",
            "next_action": "Start collection.",
        }
    return {
        "message_id": args.message_id,
        "status": record["status"],
        "downloaded_bytes": record["downloaded_bytes"],
        "expected_size": record["expected_size"],
        "last_successful_offset": record["last_successful_offset"],
        "attempt_count": record["attempt_count"],
        "next_action": record.get("next_action") or "",
    }


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
        "Storage mode": "LOCAL ONLY",
        "Output root": str(settings.output_root),
        "Cloud uploads": "DISABLED",
        "Yandex Object Storage": "NOT USED",
        "telegram_credentials_present": settings.has_telegram_credentials,
        "ffmpeg": bool(executable("ffmpeg")),
        "ffprobe": bool(executable("ffprobe")),
        "ffmpeg_version": executable_version("ffmpeg"),
        "ffprobe_version": executable_version("ffprobe"),
        "free_disk_bytes": shutil.disk_usage(settings.output_root).free,
        "output_directories": all(path.exists() for path in paths.values()),
    }
    try:
        from tools.research.wst.ocr import (
            active_ocr_backend,
            available_ocr_backends,
            available_ocr_languages,
        )

        languages = available_ocr_languages()
        report["ocr_languages"] = sorted(languages)
        report["ocr_rus_eng"] = {"rus", "eng"}.issubset(languages)
        report["ocr_backends"] = available_ocr_backends()
        report["OCR backend"] = active_ocr_backend() or "NOT READY"
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
        elif args.command == "bootstrap-media":
            result = bootstrap_media(
                install=args.install,
                whisper_model=args.whisper_model,
                download_model=args.download_model,
            )
        elif args.command == "bootstrap-ocr":
            result = bootstrap_ocr(
                install=args.install,
                backend=args.backend,
                download_model=args.download_model,
            )
        elif args.command == "retry-media":
            result = retry_media(args)
        elif args.command == "download-status":
            result = download_status(args)
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
