import pytest

from apps.operations.backup_budget import BudgetError, Generation, reserve_capacity


class FakeDestination:
    def __init__(self, sizes, verified, *, hidden=0, deletion_releases=True):
        self.sizes = sizes.copy()
        self.verified = set(verified)
        self.hidden = hidden
        self.deletion_releases = deletion_releases
        self.deleted = []

    def physical_bytes(self):
        return sum(self.sizes.values()) + self.hidden

    def generations(self):
        return [Generation(name, name in self.verified) for name in self.sizes]

    def delete_generation(self, name):
        self.deleted.append(name)
        if self.deletion_releases:
            self.sizes.pop(name)


def test_exact_limit_does_not_rotate():
    store = FakeDestination({"a": 600_000_000}, {"a"})
    assert reserve_capacity(store, 400_000_000) == []


def test_preflight_never_deletes_verified_generations():
    store = FakeDestination({"a": 350, "b": 350, "c": 200}, {"a", "b", "c"})
    with pytest.raises(BudgetError, match="не помещается"):
        reserve_capacity(store, 450, limit=1000)
    assert store.deleted == []
    assert "c" in store.sizes


def test_versions_and_partials_count_even_if_not_visible_as_generations():
    store = FakeDestination({"a": 300, "b": 300}, {"a", "b"}, hidden=300)
    with pytest.raises(BudgetError, match="не помещается"):
        reserve_capacity(store, 600, limit=1000)
    assert store.deleted == []
    assert "b" in store.sizes


def test_versioned_delete_marker_does_not_claim_space_was_freed():
    store = FakeDestination({"a": 400, "b": 400}, {"a", "b"}, deletion_releases=False)
    with pytest.raises(BudgetError, match="не помещается"):
        reserve_capacity(store, 300, limit=1000)
    assert store.deleted == []


def test_no_verified_generation_fails_without_deletion():
    store = FakeDestination({"partial": 100}, set())
    with pytest.raises(BudgetError, match="не помещается"):
        reserve_capacity(store, 950, limit=1000)
    assert store.deleted == []


def test_other_destination_failure_cannot_mutate_this_destination():
    yandex = FakeDestination({"a": 600}, {"a"})
    drive = FakeDestination({"b": 600}, {"b"})
    with pytest.raises(BudgetError):
        reserve_capacity(drive, 700, limit=1000)
    assert yandex.sizes == {"a": 600}


@pytest.mark.parametrize("size", [-1, 1_000_000_001])
def test_invalid_incoming_size_is_rejected(size):
    store = FakeDestination({"a": 100}, {"a"})
    with pytest.raises(BudgetError):
        reserve_capacity(store, size)
