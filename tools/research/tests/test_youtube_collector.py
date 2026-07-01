from __future__ import annotations

from typing import Any

import pytest

from tools.research import youtube_collector
from tools.research.youtube_collector import (
    YouTubeApiError,
    collect_comments_for_video,
    is_comments_disabled_error,
)


def test_comments_disabled_is_limitation(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network should not be used in this test")

    def fake_fetch(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        assert endpoint == "commentThreads"
        assert params["videoId"] == "v1"
        raise YouTubeApiError(403, "commentsDisabled", "Comments are disabled")

    monkeypatch.setattr(youtube_collector.urllib.request, "urlopen", fail_network)

    result = collect_comments_for_video(
        fake_fetch,
        {"video_id": "v1", "url": "https://www.youtube.com/watch?v=v1"},
        comment_limit=10,
    )

    assert result.comments == []
    assert result.api_calls == 0
    assert result.limitations == ["YouTube video v1: commentsDisabled."]


def test_is_comments_disabled_error() -> None:
    assert is_comments_disabled_error(YouTubeApiError(403, "commentsDisabled", "disabled"))
    assert not is_comments_disabled_error(YouTubeApiError(403, "quotaExceeded", "quota"))
