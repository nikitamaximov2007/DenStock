from pathlib import Path

import pytest
from django.test import override_settings

from apps.operations.offline_cutover import CutoverError, _file_slot_plan


@pytest.mark.parametrize(
    ("slot", "setting"),
    (("media", "MEDIA_ROOT"), ("private_media", "PRIVATE_MEDIA_ROOT")),
)
def test_cutover_refuses_a_docker_volume_mountpoint_without_mutating_it(
    tmp_path, monkeypatch, slot, setting
):
    volume_data = tmp_path / f"{slot}-volume" / "_data"
    live = volume_data / "mounted-root"
    staged = volume_data / "staged-root"
    live.mkdir(parents=True)
    staged.mkdir()
    (live / "keep.txt").write_text("old generation", encoding="utf-8")
    (staged / "candidate.txt").write_text("new generation", encoding="utf-8")
    before = sorted(path.name for path in volume_data.iterdir())
    monkeypatch.setattr(
        "apps.operations.offline_cutover.os.path.ismount",
        lambda value: Path(value) == live,
    )

    with override_settings(**{setting: live}):
        with pytest.raises(CutoverError, match="является точкой монтирования"):
            _file_slot_plan(slot, str(staged), "synthetic-cutover-id")

    assert sorted(path.name for path in volume_data.iterdir()) == before
    assert (live / "keep.txt").read_text(encoding="utf-8") == "old generation"
    assert (staged / "candidate.txt").read_text(encoding="utf-8") == "new generation"
