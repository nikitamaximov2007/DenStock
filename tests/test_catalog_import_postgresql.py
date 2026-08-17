"""PostgreSQL-only concurrency guarantees for catalog import."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest
from django.db import close_old_connections, connection

from apps.catalog_import.adapters import file_sha256
from apps.catalog_import.models import CatalogImportBatch
from apps.catalog_import.services import CatalogImportError, apply_batch

pytestmark = pytest.mark.postgresql

if connection.vendor != "postgresql":
    pytest.skip(
        "Run against PostgreSQL 16 with DENSTOCK_TEST_DATABASE_URL",
        allow_module_level=True,
    )


class _CoordinatedAdapter:
    """Makes the old pre-lock fingerprint race deterministic."""

    def __init__(self):
        self.fingerprint_value = "base"
        self.first_apply_entered = Event()
        self.second_apply_entered = Event()
        self.release_first_apply = Event()
        self._calls = 0
        self._lock = Lock()

    def fingerprint(self):
        return self.fingerprint_value

    def apply(self, path):
        with self._lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self.first_apply_entered.set()
            assert self.release_first_apply.wait(10)
            self.fingerprint_value = "after-first-apply"
        else:
            self.second_apply_entered.set()
        return {"applied": path.name}


def _thread_apply(batch_id):
    close_old_connections()
    try:
        batch = CatalogImportBatch.objects.get(pk=batch_id)
        return apply_batch(batch)
    except Exception as exc:  # noqa: BLE001 - result is asserted by the caller.
        return exc
    finally:
        close_old_connections()


def _checked_batch(root: Path, name: str) -> CatalogImportBatch:
    path = root / name
    path.write_bytes(name.encode())
    return CatalogImportBatch.objects.create(
        catalog=CatalogImportBatch.Catalog.BRP,
        status=CatalogImportBatch.Status.CHECKED,
        source_filename=name,
        source_sha256=file_sha256(path),
        source_size=path.stat().st_size,
        stored_path=name,
        catalog_fingerprint="base",
    )


@pytest.mark.django_db(transaction=True)
def test_postgresql_concurrent_apply_rechecks_catalog_after_shared_lock(settings, monkeypatch, tmp_path):
    """A second checked import must become stale after the first changes catalog."""
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    root = Path(settings.PRIVATE_MEDIA_ROOT) / "catalog-imports"
    root.mkdir(parents=True)
    first = _checked_batch(root, "first.xlsx")
    second = _checked_batch(root, "second.xlsx")
    adapter = _CoordinatedAdapter()
    monkeypatch.setattr("apps.catalog_import.services.get_adapter", lambda catalog: adapter)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(_thread_apply, first.pk)
        assert adapter.first_apply_entered.wait(10)
        second_future = pool.submit(_thread_apply, second.pk)
        try:
            # The pre-fix implementation lets both transactions enter apply().
            assert not adapter.second_apply_entered.wait(0.5)
        finally:
            adapter.release_first_apply.set()
        first_result = first_future.result(timeout=10)
        second_result = second_future.result(timeout=10)

    assert isinstance(first_result, CatalogImportBatch)
    assert isinstance(second_result, CatalogImportError)
    assert "STALE_DRY_RUN" in str(second_result)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == CatalogImportBatch.Status.APPLIED
    assert second.status == CatalogImportBatch.Status.CHECKED
