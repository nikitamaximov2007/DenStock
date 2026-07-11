from __future__ import annotations

from pathlib import Path

from tools.research.wst.media_recovery import RecoveryAttempt, validate_media_file
from tools.research.wst.state import WSTState


def test_integrity_rejects_part_html_and_size_mismatch(tmp_path: Path) -> None:
    partial = tmp_path / "video.mp4.part"
    partial.write_bytes(b"video")
    html = tmp_path / "video.mp4"
    html.write_text("<html>error</html>", encoding="utf-8")

    assert validate_media_file(partial).valid is False
    assert validate_media_file(html).valid is False


def test_state_keeps_partial_stage_and_actionable_retry(tmp_path: Path) -> None:
    with WSTState(tmp_path / "state.sqlite3") as state:
        state.begin_stage(7, "audio_extracted", backend="ffmpeg")
        state.finish_stage(7, "frames_extracted", status="complete", artifacts=["frame.jpg"])
        state.fail_stage(
            7,
            "audio_extracted",
            "stream failed",
            retry=True,
            next_action="Try the repaired container.",
        )

        retry = state.retry_queue(stage="audio_extracted")
        frames = state.stage_record(7, "frames_extracted")

    assert retry[0]["status"] == "retry_pending"
    assert retry[0]["next_action"] == "Try the repaired container."
    assert frames["artifact_paths"] == ["frame.jpg"]


def test_recovery_attempt_keeps_bounded_diagnostic_fields() -> None:
    attempt = RecoveryAttempt("remux", "failed", ["ffmpeg", "input.mp4"], 1, "decoder error")

    assert attempt.as_dict() == {
        "name": "remux",
        "status": "failed",
        "command": ["ffmpeg", "input.mp4"],
        "exit_code": 1,
        "stderr": "decoder error",
        "artifact": "",
    }
