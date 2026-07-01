from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .sanitize import ensure_research_tree, write_jsonl
except ImportError:  # pragma: no cover - direct script execution
    from sanitize import ensure_research_tree, write_jsonl

CHANNEL_ID = "UCdgOSRp40M8rf5L9iZ4tzOg"
API_BASE = "https://www.googleapis.com/youtube/v3"

JsonFetcher = Callable[[str, Mapping[str, Any]], dict[str, Any]]


class YouTubeApiError(RuntimeError):
    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(f"YouTube API error {status}: {reason or message}")
        self.status = status
        self.reason = reason
        self.message = message


@dataclass
class YouTubeCollectionResult:
    videos: int = 0
    comments: int = 0
    quota_units: int = 0
    limitations: list[str] = field(default_factory=list)

    def as_report(self) -> dict[str, Any]:
        return {
            "counts": {
                "youtube_videos": self.videos,
                "youtube_comments": self.comments,
                "youtube_api_calls_estimate": self.quota_units,
            },
            "limitations": self.limitations,
            "notes": [
                "YouTube was collected through Data API v3 with an API key, no OAuth.",
                "Author names, author channel IDs, avatars, and profile links are not written.",
            ],
        }


@dataclass
class CommentCollectionResult:
    comments: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    api_calls: int = 0


def fetch_api_json(api_key: str, endpoint: str, params: Mapping[str, Any]) -> dict[str, Any]:
    query = dict(params)
    query["key"] = api_key
    url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = _load_error_payload(exc)
        reason = _extract_error_reason(payload)
        message = _extract_error_message(payload)
        raise YouTubeApiError(exc.code, reason, message) from exc
    except urllib.error.URLError as exc:
        raise YouTubeApiError(0, "networkError", str(exc.reason)) from exc


def collect_youtube(
    api_key: str,
    project_root: Path | None = None,
    video_limit: int = 50,
    comment_limit: int = 100,
    include_comments: bool = True,
    fetch_json: JsonFetcher | None = None,
) -> YouTubeCollectionResult:
    if not api_key:
        raise ValueError("YT_API_KEY is required for YouTube collection")

    paths = ensure_research_tree(project_root)
    fetcher = fetch_json or (lambda endpoint, params: fetch_api_json(api_key, endpoint, params))
    result = YouTubeCollectionResult()

    uploads_playlist_id, calls = get_uploads_playlist_id(fetcher)
    result.quota_units += calls

    videos, calls = collect_video_records(fetcher, uploads_playlist_id, video_limit)
    result.quota_units += calls
    result.videos = write_jsonl(
        videos,
        paths["raw"] / "youtube_videos.jsonl",
        "youtube_videos",
        project_root,
    )

    comments: list[dict[str, Any]] = []
    if include_comments:
        for video in videos:
            comment_result = collect_comments_for_video(fetcher, video, comment_limit)
            comments.extend(comment_result.comments)
            result.limitations.extend(comment_result.limitations)
            result.quota_units += comment_result.api_calls
    else:
        result.limitations.append("YouTube comments skipped by --no-comments/--posts-only.")

    result.comments = write_jsonl(
        comments,
        paths["raw"] / "youtube_comments.jsonl",
        "youtube_comments",
        project_root,
    )
    return result


def get_uploads_playlist_id(fetch_json: JsonFetcher) -> tuple[str, int]:
    response = fetch_json(
        "channels",
        {
            "part": "contentDetails",
            "id": CHANNEL_ID,
            "maxResults": 1,
        },
    )
    items = response.get("items", [])
    if not items:
        raise YouTubeApiError(404, "channelNotFound", f"Channel not found: {CHANNEL_ID}")
    uploads = (
        items[0]
        .get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads:
        raise YouTubeApiError(404, "uploadsPlaylistNotFound", "Uploads playlist not found")
    return str(uploads), 1


def collect_video_records(
    fetch_json: JsonFetcher,
    uploads_playlist_id: str,
    video_limit: int,
) -> tuple[list[dict[str, Any]], int]:
    videos: list[dict[str, Any]] = []
    calls = 0
    page_token: str | None = None

    while len(videos) < video_limit:
        page_size = min(50, max(1, video_limit - len(videos)))
        params: dict[str, Any] = {
            "part": "snippet",
            "playlistId": uploads_playlist_id,
            "maxResults": page_size,
        }
        if page_token:
            params["pageToken"] = page_token

        response = fetch_json("playlistItems", params)
        calls += 1
        videos.extend(parse_playlist_item(item) for item in response.get("items", []))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    details, detail_calls = fetch_video_details(fetch_json, videos)
    calls += detail_calls
    for video in videos:
        video.update(details.get(str(video.get("video_id")), {}))

    return videos[:video_limit], calls


def parse_playlist_item(item: Mapping[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet", {})
    video_id = snippet.get("resourceId", {}).get("videoId")
    return {
        "source": "youtube",
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "date": snippet.get("publishedAt"),
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
    }


def fetch_video_details(
    fetch_json: JsonFetcher,
    videos: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int]:
    video_ids = [str(video.get("video_id")) for video in videos if video.get("video_id")]
    details: dict[str, dict[str, Any]] = {}
    calls = 0

    for start in range(0, len(video_ids), 50):
        batch = video_ids[start : start + 50]
        response = fetch_json(
            "videos",
            {
                "part": "snippet,statistics",
                "id": ",".join(batch),
                "maxResults": len(batch),
            },
        )
        calls += 1
        for item in response.get("items", []):
            video_id = str(item.get("id", ""))
            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            details[video_id] = {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "view_count": _safe_int_or_none(statistics.get("viewCount")),
                "like_count": _safe_int_or_none(statistics.get("likeCount")),
                "comment_count": _safe_int_or_none(statistics.get("commentCount")),
            }

    return details, calls


def collect_comments_for_video(
    fetch_json: JsonFetcher,
    video: Mapping[str, Any],
    comment_limit: int,
) -> CommentCollectionResult:
    video_id = str(video.get("video_id", ""))
    video_url = str(video.get("url", ""))
    result = CommentCollectionResult()
    page_token: str | None = None

    while len(result.comments) < comment_limit:
        page_size = min(100, max(1, comment_limit - len(result.comments)))
        params: dict[str, Any] = {
            "part": "snippet,replies",
            "videoId": video_id,
            "maxResults": page_size,
            "order": "relevance",
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            response = fetch_json("commentThreads", params)
            result.api_calls += 1
        except YouTubeApiError as exc:
            if is_comments_disabled_error(exc):
                result.limitations.append(f"YouTube video {video_id}: commentsDisabled.")
                return result
            result.limitations.append(
                f"YouTube video {video_id}: comments unavailable ({exc.reason})."
            )
            return result

        for item in response.get("items", []):
            if len(result.comments) >= comment_limit:
                break
            top_level = item.get("snippet", {}).get("topLevelComment", {})
            top_snippet = top_level.get("snippet", {})
            result.comments.append(parse_comment_snippet(top_snippet, video_id, video_url, False))

            for reply in item.get("replies", {}).get("comments", []):
                if len(result.comments) >= comment_limit:
                    break
                reply_snippet = reply.get("snippet", {})
                result.comments.append(
                    parse_comment_snippet(reply_snippet, video_id, video_url, True)
                )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return result


def parse_comment_snippet(
    snippet: Mapping[str, Any],
    video_id: str,
    video_url: str,
    is_reply: bool,
) -> dict[str, Any]:
    return {
        "source": "youtube",
        "video_id": video_id,
        "video_url": video_url,
        "date": snippet.get("publishedAt"),
        "text": snippet.get("textOriginal") or snippet.get("textDisplay", ""),
        "like_count": _safe_int_or_none(snippet.get("likeCount")),
        "is_reply": is_reply,
    }


def is_comments_disabled_error(error: YouTubeApiError) -> bool:
    return error.status == 403 and error.reason == "commentsDisabled"


def _load_error_payload(error: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        return json.loads(error.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _extract_error_reason(payload: Mapping[str, Any]) -> str:
    error = payload.get("error", {})
    errors = error.get("errors", []) if isinstance(error, Mapping) else []
    if errors:
        first = errors[0]
        if isinstance(first, Mapping):
            return str(first.get("reason", ""))
    if isinstance(error, Mapping):
        return str(error.get("status", ""))
    return ""


def _extract_error_message(payload: Mapping[str, Any]) -> str:
    error = payload.get("error", {})
    if isinstance(error, Mapping):
        return str(error.get("message", ""))
    return ""


def _safe_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
