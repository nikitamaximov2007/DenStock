import sys
from pathlib import Path

import pytest

NETWORK_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "ai-support"
sys.path.insert(0, str(NETWORK_SCRIPTS))


@pytest.fixture
def project_root():
    return Path(__file__).resolve().parents[2]
