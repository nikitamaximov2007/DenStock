"""Global test-process isolation for mutable deployment settings."""

import pytest


@pytest.fixture(autouse=True)
def restore_test_deployment_mode(settings):
    """Prevent a test's direct settings mutation from leaking into fixture restore."""
    settings.DENSTOCK_MODE = "test"
    yield
    settings.DENSTOCK_MODE = "test"
