"""Run the dependency-free browser keyboard handler tests when Node is available."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "script_name",
    ["quick_actions_keyboard.test.js", "customer_requests_realtime.test.js"],
)
def test_browser_scripts(script_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not available")
    source = Path(__file__).parent / "js" / script_name
    result = subprocess.run(
        [node, "--test", str(source)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
