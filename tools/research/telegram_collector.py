from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .sanitize import ensure_research_tree, write_jsonl
except ImportError:  # pragma: no cover - direct script execution
    from sanitize import ensure_research_tree, write_jsonl

CHANNEL_USERNAME = "probrp1"
CHANNEL_URL = "https://t.me/probrp1"
SESSION_DIR = Path(__file__).resolve().parent / ".sessions"
SESSION_NAME = "denis_telegram_readonly"


@dataclass
class TelegramCollectionResult:
    posts: int = 0
    comments: int = 0
    limitations: list[str] = field(default_factory=list)

    def as_report(self) -> dict[str, Any]:
        return {
            "counts": {
                "telegram_posts": self.posts,
                "telegram_comments": self.comments,
            },
            "limitations": self.limitations,
            "notes": [
                "Telegram was collected through Telethon read-only calls.",
                "Author IDs, names, usernames, avatars, and profile links are not written.",
            ],
        }


def default_session_path() -> Path:
    return SESSION_DIR / SESSION_NAME


def run_telegram_collection(
    api_id: str,
    api_hash: str,
    project_root: Path | None = None,
    post_limit: int = 50,
    comment_limit: int = 100,
    include_comments: bool = True,
    session_path: Path | None = None,
) -> TelegramCollectionResult:
    return asyncio.run(
        collect_telegram(
            api_id=api_id,
            api_hash=api_hash,
            project_root=project_root,
            post_limit=post_limit,
            comment_limit=comment_limit,
            include_comments=include_comments,
            session_path=session_path,
        )
    )


async def collect_telegram(
    api_id: str,
    api_hash: str,
    project_root: Path | None = None,
    post_limit: int = 50,
    comment_limit: int = 100,
    include_comments: bool = True,
    session_path: Path | None = None,
) -> TelegramCollectionResult:
    if not api_id or not api_hash:
        raise ValueError("TG_API_ID and TG_API_HASH are required for Telegram collection")

    telegram_client = _load_telegram_client()
    session = session_path or default_session_path()
    session.parent.mkdir(parents=True, exist_ok=True)

    paths = ensure_research_tree(project_root)
    result = TelegramCollectionResult()
    posts: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []

    client = telegram_client(str(session), int(api_id), api_hash)
    async with client:
        await client.start()
        channel = await client.get_entity(CHANNEL_USERNAME)

        async for message in client.iter_messages(channel, limit=post_limit):
            post = telegram_post_record(message)
            posts.append(post)

            if not include_comments:
                continue
            comment_result = await collect_comments_for_post(
                client,
                channel,
                post,
                comment_limit,
            )
            comments.extend(comment_result["comments"])
            result.limitations.extend(comment_result["limitations"])

    if not include_comments:
        result.limitations.append("Telegram comments skipped by --no-comments/--posts-only.")

    result.posts = write_jsonl(
        posts,
        paths["raw"] / "telegram_posts.jsonl",
        "telegram_posts",
        project_root,
    )
    result.comments = write_jsonl(
        comments,
        paths["raw"] / "telegram_comments.jsonl",
        "telegram_comments",
        project_root,
    )
    return result


async def collect_comments_for_post(
    client: Any,
    channel: Any,
    post: Mapping[str, Any],
    comment_limit: int,
) -> dict[str, Any]:
    post_id = post.get("post_id")
    limitations: list[str] = []
    comments: list[dict[str, Any]] = []
    if not post_id:
        return {"comments": comments, "limitations": ["Telegram post without id skipped."]}

    try:
        async for message in client.iter_messages(channel, reply_to=post_id, limit=comment_limit):
            if getattr(message, "id", None) == post_id:
                continue
            comments.append(telegram_comment_record(message, post))
    except Exception as exc:  # noqa: BLE001
        reason = exc.__class__.__name__
        limitations.append(f"Telegram post {post_id}: comments unavailable ({reason}).")

    return {"comments": comments, "limitations": limitations}


def telegram_post_record(message: Any) -> dict[str, Any]:
    post_id = getattr(message, "id", None)
    return {
        "source": "telegram",
        "post_id": post_id,
        "url": f"{CHANNEL_URL}/{post_id}",
        "date": _iso_datetime(getattr(message, "date", None)),
        "text": _message_text(message),
        "has_photo": bool(getattr(message, "photo", None)),
        "has_video": _has_video(message),
        "views": _safe_int_or_none(getattr(message, "views", None)),
        "forwards": _safe_int_or_none(getattr(message, "forwards", None)),
        "grouped_id": getattr(message, "grouped_id", None),
    }


def telegram_comment_record(message: Any, post: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": "telegram",
        "parent_post_id": post.get("post_id"),
        "parent_url": post.get("url"),
        "date": _iso_datetime(getattr(message, "date", None)),
        "text": _message_text(message),
        "reactions_count": _reaction_count(message),
    }


def _load_telegram_client() -> Any:
    try:
        from telethon import TelegramClient
    except ImportError as exc:  # pragma: no cover - depends on research venv
        msg = (
            "Install research dependencies: "
            "pip install -r tools/research/requirements-research.txt"
        )
        raise RuntimeError(msg) from exc
    return TelegramClient


def _message_text(message: Any) -> str:
    return str(getattr(message, "message", None) or getattr(message, "text", "") or "")


def _has_video(message: Any) -> bool:
    if getattr(message, "video", None):
        return True
    document = getattr(message, "document", None)
    mime_type = getattr(document, "mime_type", "") if document else ""
    return str(mime_type).startswith("video/")


def _reaction_count(message: Any) -> int | None:
    reactions = getattr(message, "reactions", None)
    results = getattr(reactions, "results", None)
    if not results:
        return None
    count = 0
    for item in results:
        count += int(getattr(item, "count", 0) or 0)
    return count


def _iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def _safe_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
