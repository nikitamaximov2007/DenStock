"""Phone-identity duplicate audit + safe explicit Customer merge.

Test matrix (see the task spec this implements):

AUDIT (read-only classification, never merges/writes):
  1. equivalent Russian phone formats group together
  2. different canonical phones do not group
  3. missing phone is counted separately, never a "duplicate"
  4. invalid phone (text with no digits) is counted separately
  5. duplicate group per-customer relation counts are correct
  6. conflicting names -> REVIEW_REQUIRED; compatible names -> SAFE_LIKELY_DUPLICATE

MERGE (explicit target/source, dry-run by default):
  7. preview (dry-run) never writes
  8-10. apply moves Sales / Repairs / CustomerRequests
  11. historical snapshots (customer_name/customer_phone) are untouched
  12. money (revenue_total) is untouched
  13. stock/StockMovement is untouched
  14. target/source are always explicit - never auto-picked
  15. source lifecycle: tombstoned (merged_into), never deleted, still viewable
  16. a durable CustomerMergeReceipt audit trail is written
  17. a second apply of the exact same pair is idempotent (no double move)
  18. a source already merged into a DIFFERENT target fails closed
  19. a target that is itself an already-merged source fails closed
  20. concurrent merge safety (PostgreSQL advisory lock + row locking)
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.utils import timezone

from apps.customer_requests.models import CustomerRequest
from apps.customers.dedup_audit import (
    REVIEW_REQUIRED,
    SAFE_LIKELY_DUPLICATE,
    audit_customer_phone_duplicates,
)
from apps.customers.merge import (
    CustomerMergeError,
    execute_customer_merge,
    preview_customer_merge,
)
from apps.customers.models import Customer, CustomerMergeReceipt
from apps.inventory.models import StockMovement
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def admin(make_user):
    return make_user("audit-admin", is_superuser=True)


def _sale(customer, *, revenue="1500.00"):
    return Sale.objects.create(
        status=Sale.Status.COMPLETED,
        customer=customer,
        customer_name=customer.name,
        customer_phone=customer.phone,
        revenue_total=Decimal(revenue),
        sold_at=timezone.now(),
    )


def _repair(customer):
    return RepairOrder.objects.create(
        status=RepairOrder.Status.COMPLETED,
        customer=customer,
        customer_name=customer.name,
        customer_phone=customer.phone,
    )


def _customer_request(customer, *, key="req"):
    return CustomerRequest.objects.create(
        customer=customer,
        customer_name=customer.name,
        customer_phone=customer.phone,
        preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
        privacy_policy_version="v1",
        personal_data_consent_version="v1",
        consent_purpose="test",
        consent_accepted_at=timezone.now(),
        submission_key_hash=f"hash-{key}",
    )


# --- AUDIT -------------------------------------------------------------------------------


def test_audit_groups_equivalent_phone_formats(db):
    Customer.objects.create(name="Иванов", phone="+7 900 111-22-33")
    Customer.objects.create(name="Иванов И.И.", phone="8 (900) 111-22-33")

    report = audit_customer_phone_duplicates()

    assert report.duplicate_groups == 1
    group = report.groups[0]
    assert group.normalized_phone == "79001112233"
    assert {row.customer_id for row in group.customers} == set(
        Customer.objects.values_list("pk", flat=True)
    )


def test_audit_does_not_group_different_phones(db):
    Customer.objects.create(name="Первый", phone="+7 900 111-22-33")
    Customer.objects.create(name="Второй", phone="+7 900 999-88-77")

    report = audit_customer_phone_duplicates()

    assert report.duplicate_groups == 0


def test_audit_counts_missing_phone_separately(db):
    Customer.objects.create(name="Без телефона")
    Customer.objects.create(name="С телефоном", phone="+7 900 111-22-33")

    report = audit_customer_phone_duplicates()

    assert report.missing_phone == 1
    assert report.duplicate_groups == 0


def test_audit_counts_invalid_phone_separately(db):
    Customer.objects.create(name="Некорректный", phone="не указан")

    report = audit_customer_phone_duplicates()

    assert report.invalid_phone == 1
    assert report.missing_phone == 0
    assert report.duplicate_groups == 0


def test_audit_group_relation_counts_are_correct(db, admin):
    first = Customer.objects.create(name="Первый", phone="+7 900 111-22-33")
    second = Customer.objects.create(name="Второй", phone="8 900 111 22 33")
    _sale(first)
    _sale(first)
    _repair(second)

    report = audit_customer_phone_duplicates()

    group = report.groups[0]
    by_id = {row.customer_id: row for row in group.customers}
    assert by_id[first.pk].sale_count == 2
    assert by_id[first.pk].repair_count == 0
    assert by_id[second.pk].sale_count == 0
    assert by_id[second.pk].repair_count == 1


def test_audit_classifies_conflicting_names_as_review_required(db):
    Customer.objects.create(name="Александр", phone="+7 900 111-22-33")
    Customer.objects.create(name="Мария", phone="8 900 111 22 33")

    report = audit_customer_phone_duplicates()

    group = report.groups[0]
    assert group.classification == REVIEW_REQUIRED
    assert report.groups_with_conflicting_names == 1


def test_audit_classifies_compatible_names_as_safe(db):
    Customer.objects.create(name="Александр Пушкарёв", phone="+7 900 111-22-33")
    Customer.objects.create(name="Александр", phone="8 900 111 22 33")

    report = audit_customer_phone_duplicates()

    group = report.groups[0]
    assert group.classification == SAFE_LIKELY_DUPLICATE
    assert report.groups_with_conflicting_names == 0


# --- MERGE ---------------------------------------------------------------------------------


def test_merge_preview_never_writes(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    sale = _sale(source)

    plan = preview_customer_merge(target, source)

    assert plan.moved_counts["sales"] == 1
    sale.refresh_from_db()
    assert sale.customer_id == source.pk
    source.refresh_from_db()
    assert source.merged_into_id is None
    assert not CustomerMergeReceipt.objects.exists()


def test_merge_apply_moves_sales(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    sale = _sale(source)

    execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)

    sale.refresh_from_db()
    assert sale.customer_id == target.pk


def test_merge_apply_moves_repairs(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    repair = _repair(source)

    execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)

    repair.refresh_from_db()
    assert repair.customer_id == target.pk


def test_merge_apply_moves_customer_requests(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    request = _customer_request(source, key="merge-move")

    execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)

    request.refresh_from_db()
    assert request.customer_id == target.pk


def test_merge_preserves_historical_snapshots(db, admin):
    """Only the live FK reference moves - the frozen name/phone text never does."""
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Иван Источников", phone="8 900 111 22 33")
    sale = _sale(source)

    execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)

    sale.refresh_from_db()
    assert sale.customer_name == "Иван Источников"
    assert sale.customer_phone == "8 900 111 22 33"


def test_merge_preserves_money(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    sale = _sale(source, revenue="12345.67")

    execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)

    sale.refresh_from_db()
    assert sale.revenue_total == Decimal("12345.67")


def test_merge_does_not_touch_stock(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    _sale(source)
    before = set(StockMovement.objects.values_list("pk", flat=True))

    execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)

    after = set(StockMovement.objects.values_list("pk", flat=True))
    assert before == after


def test_merge_rejects_self_merge(db, admin):
    customer = Customer.objects.create(name="Сам себе", phone="+7 900 111-22-33")

    with pytest.raises(CustomerMergeError):
        execute_customer_merge(target_id=customer.pk, source_id=customer.pk, by=admin)


def test_merge_source_is_tombstoned_not_deleted(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")

    execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)

    source.refresh_from_db()
    assert source.merged_into_id == target.pk
    assert source.is_merged
    assert Customer.objects.filter(pk=source.pk).exists()


def test_merge_writes_a_receipt(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    _sale(source)
    _repair(source)

    receipt = execute_customer_merge(
        target_id=target.pk, source_id=source.pk, by=admin, reason="дубль по телефону"
    )

    assert receipt.target_id == target.pk
    assert receipt.source_id == source.pk
    assert receipt.normalized_phone == "79001112233"
    assert receipt.performed_by_id == admin.pk
    assert receipt.reason == "дубль по телефону"
    assert receipt.moved_counts["sales"] == 1
    assert receipt.moved_counts["repairs"] == 1


def test_merge_apply_twice_is_idempotent(db, admin):
    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    sale = _sale(source)

    first = execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)
    second = execute_customer_merge(target_id=target.pk, source_id=source.pk, by=admin)

    assert first.pk == second.pk
    assert CustomerMergeReceipt.objects.filter(source=source, target=target).count() == 1
    sale.refresh_from_db()
    assert sale.customer_id == target.pk


def test_merge_source_already_merged_into_different_target_fails_closed(db, admin):
    first_target = Customer.objects.create(name="Первая цель", phone="+7 900 111-22-33")
    other_target = Customer.objects.create(name="Другая цель", phone="+7 900 999-88-77")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    execute_customer_merge(target_id=first_target.pk, source_id=source.pk, by=admin)

    with pytest.raises(CustomerMergeError):
        execute_customer_merge(target_id=other_target.pk, source_id=source.pk, by=admin)


def test_merge_target_already_merged_away_fails_closed(db, admin):
    canonical = Customer.objects.create(name="Каноническая", phone="+7 900 111-22-33")
    now_tombstoned = Customer.objects.create(name="Уже объединена", phone="8 900 111 22 33")
    third = Customer.objects.create(name="Третья", phone="+7 900 999-88-77")
    execute_customer_merge(target_id=canonical.pk, source_id=now_tombstoned.pk, by=admin)

    with pytest.raises(CustomerMergeError):
        execute_customer_merge(target_id=now_tombstoned.pk, source_id=third.pk, by=admin)


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_merge_concurrent_same_pair_moves_relations_once():
    if connection.vendor != "postgresql":
        pytest.skip("Run against PostgreSQL with DENSTOCK_TEST_DATABASE_URL")
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections

    target = Customer.objects.create(name="Целевой", phone="+7 900 111-22-33")
    source = Customer.objects.create(name="Источник", phone="8 900 111 22 33")
    _sale(source)

    def apply_in_separate_connection():
        close_old_connections()
        try:
            receipt = execute_customer_merge(target_id=target.pk, source_id=source.pk)
            return receipt.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=20)
            for future in (
                pool.submit(apply_in_separate_connection),
                pool.submit(apply_in_separate_connection),
            )
        ]

    assert results[0] == results[1]
    assert CustomerMergeReceipt.objects.filter(source=source, target=target).count() == 1
    assert Sale.objects.filter(customer=target).count() == 1
