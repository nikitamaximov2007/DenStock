"""Global test-process isolation for mutable deployment settings."""

import pytest
from django.core.management import call_command
from django.db import connections
from django.test import TransactionTestCase


@pytest.fixture(autouse=True)
def restore_test_deployment_mode(settings):
    """Prevent a test's direct settings mutation from leaking into fixture restore."""
    settings.DENSTOCK_MODE = "test"
    yield
    settings.DENSTOCK_MODE = "test"


_transaction_fixture_setup = TransactionTestCase._fixture_setup.__func__


@classmethod
def _fixture_setup_with_clean_serialized_restore(cls):
    """Flush before Django restores a serialized PostgreSQL test fixture.

    A preceding TransactionTestCase runs ``post_migrate`` during its flush.
    Django's own serialized restore then inserts content types into those
    populated tables and fails with duplicate keys. The restored fixture is
    authoritative, so it must be applied to an empty database.
    """
    if cls.serialized_rollback:
        for db_name in cls._databases_names(include_mirrors=False):
            if hasattr(connections[db_name], "_test_serialized_contents"):
                call_command(
                    "flush",
                    verbosity=0,
                    interactive=False,
                    database=db_name,
                    inhibit_post_migrate=True,
                )
    return _transaction_fixture_setup(cls)


TransactionTestCase._fixture_setup = _fixture_setup_with_clean_serialized_restore
