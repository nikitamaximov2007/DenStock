from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tools.research.telegram_collector import telegram_comment_record, telegram_post_record


def test_telegram_records_drop_author_data() -> None:
    message = SimpleNamespace(
        id=12,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        message="Question from @private_user about delivery",
        photo=True,
        video=None,
        document=None,
        views=100,
        forwards=5,
        grouped_id=None,
        sender_id=999,
        author="Private Name",
    )

    post = telegram_post_record(message)
    comment = telegram_comment_record(message, post)

    assert "sender_id" not in post
    assert "author" not in post
    assert "sender_id" not in comment
    assert "author" not in comment
    assert post["post_id"] == 12
    assert comment["parent_post_id"] == 12
