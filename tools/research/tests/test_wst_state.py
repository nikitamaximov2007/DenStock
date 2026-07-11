from __future__ import annotations

from pathlib import Path

from tools.research.wst.state import WSTState


def _record(content_hash: str) -> dict:
    return {
        "message_id": 12,
        "content_hash": content_hash,
        "edit_date": None,
        "discovery_source": "pinned_navigation",
    }


def test_state_is_idempotent_and_resets_changed_post(tmp_path: Path) -> None:
    with WSTState(tmp_path / "wst_state.sqlite3") as state:
        assert state.record_post(_record("one")) is True
        assert state.record_post(_record("one")) is False
        assert state.record_post(_record("two")) is True
        state.update_media(12, download_status="downloaded", local_path="media/file.pdf")
        assert state.media_record(12)["download_status"] == "downloaded"


def test_state_records_navigation_edges_without_access_hash(tmp_path: Path) -> None:
    database = tmp_path / "wst_state.sqlite3"
    with WSTState(database) as state:
        state.replace_navigation_edges(
            3,
            [
                {
                    "child_message_id": 12,
                    "edge_type": "text_url",
                    "link_label": "Lesson",
                    "link_order": 1,
                }
            ],
        )

    assert "access_hash" not in database.read_bytes().decode("latin-1", errors="ignore")
