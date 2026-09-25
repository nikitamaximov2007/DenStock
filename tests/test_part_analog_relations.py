"""Relation types, verification states, provenance/evidence, audit, safety.

Companion to the already-extensive existing analog coverage:
tests/test_part_analogs.py (core link_analog/unlink_analog rules),
tests/test_part_analog_screens.py (internal UI), tests/test_analog_catalog_import.py
(importer), tests/test_analog_end_to_end.py (selling/repairing an analog keeps
separate stock, cost and history). This file covers what those don't yet:
relation_type (analog/supersession/cross_reference), verification_state
(unverified/verified/rejected), PartAnalogEvidence provenance and source
price, the read-only audit command, and the safety guarantees around
manufacturer/customs/stock/price/photo/history that a relation must never
touch merely by existing.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction

from apps.accounts import roles
from apps.actions.models import PartCustomsInfo
from apps.catalog.analog_audit import audit_part_analogs
from apps.catalog.models import PartAnalog, PartAnalogEvidence
from apps.catalog.services import (
    AnalogVerificationError,
    create_manual_part,
    link_analog,
    record_analog_evidence,
    set_analog_verification,
)
from apps.inventory.models import StockMovement
from apps.inventory.pricing import effective_part_customer_prices
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
def original(db):
    return create_manual_part(
        name="Поршень BRP", article="SAME-001", price=Decimal("10000"), manufacturer_name="BRP",
    )


@pytest.fixture
def analog(db):
    return create_manual_part(
        name="Поршень PROX", article="PROX-001", price=Decimal("6000"), manufacturer_name="PROX",
    )


# --- MODEL -------------------------------------------------------------------------------


def test_analog_relation_is_stored_with_default_type_and_state(db, original, analog):
    link, created = link_analog(original=original, analog=analog)

    assert created
    assert link.relation_type == PartAnalog.RelationType.ANALOG
    assert link.verification_state == PartAnalog.VerificationState.UNVERIFIED
    assert not link.is_confirmed


def test_supersession_is_directional(db, original, analog):
    link, _ = link_analog(
        original=original, analog=analog, relation_type=PartAnalog.RelationType.SUPERSESSION,
    )

    assert link.original_id == original.pk
    assert link.analog_id == analog.pk
    # The reverse pair is a different fact, not automatically created.
    assert not PartAnalog.objects.filter(original=analog, analog=original).exists()


def test_verification_state_starts_unverified_even_from_import_source(db, original, analog):
    link, _ = link_analog(
        original=original, analog=analog,
        source_type=PartAnalogEvidence.SourceType.MANUFACTURER_CATALOG,
    )

    assert link.verification_state == PartAnalog.VerificationState.UNVERIFIED
    assert not link.is_confirmed


def test_verification_transitions_are_explicit_and_synced(db, original, analog):
    link, _ = link_analog(original=original, analog=analog)

    set_analog_verification(link, PartAnalog.VerificationState.VERIFIED)
    link.refresh_from_db()
    assert link.is_confirmed
    assert link.verification_state == PartAnalog.VerificationState.VERIFIED

    set_analog_verification(link, PartAnalog.VerificationState.REJECTED)
    link.refresh_from_db()
    assert not link.is_confirmed
    assert link.verification_state == PartAnalog.VerificationState.REJECTED


def test_set_analog_verification_rejects_unknown_state(db, original, analog):
    link, _ = link_analog(original=original, analog=analog)

    with pytest.raises(AnalogVerificationError):
        set_analog_verification(link, "not-a-real-state")


def test_provenance_is_retained_on_the_relation(db, original, analog):
    link, _ = link_analog(
        original=original, analog=analog,
        source_type=PartAnalogEvidence.SourceType.SUPPLIER,
        source_name="Прайс поставщика Х",
        source_price=Decimal("5500"),
        source_currency="USD",
    )

    evidence = link.evidence.get()
    assert evidence.source_type == PartAnalogEvidence.SourceType.SUPPLIER
    assert evidence.source_name == "Прайс поставщика Х"
    assert evidence.source_price == Decimal("5500")
    assert evidence.source_currency == "USD"


def test_same_manufacturer_never_auto_creates_a_relation(db):
    """No fuzzy/autonomous analog creation (§6): shared manufacturer alone is nothing."""
    create_manual_part(name="Деталь один", article="X-1", manufacturer_name="PROX")
    create_manual_part(name="Деталь два", article="X-2", manufacturer_name="PROX")

    assert PartAnalog.objects.count() == 0


def test_duplicate_pair_same_type_is_rejected(db, original, analog):
    link_analog(original=original, analog=analog)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PartAnalog.objects.create(original=original, analog=analog)


def test_same_pair_different_relation_type_is_a_distinct_fact(db, original, analog):
    link_analog(original=original, analog=analog, relation_type=PartAnalog.RelationType.ANALOG)
    link2, created = link_analog(
        original=original, analog=analog, relation_type=PartAnalog.RelationType.CROSS_REFERENCE,
    )

    assert created
    assert PartAnalog.objects.filter(original=original, analog=analog).count() == 2


def test_multiple_evidence_sources_supported_for_one_relation(db, original, analog):
    link, _ = link_analog(
        original=original, analog=analog,
        source_type=PartAnalogEvidence.SourceType.MANUFACTURER_CATALOG,
        source_name="Каталог PROX 2024",
    )
    record_analog_evidence(
        link, source_type=PartAnalogEvidence.SourceType.SUPPLIER, source_name="Прайс АвтоЗапчасть",
    )

    assert link.evidence.count() == 2


def test_repeated_evidence_from_the_same_source_does_not_duplicate(db, original, analog):
    link, _ = link_analog(original=original, analog=analog)
    record_analog_evidence(link, source_type=PartAnalogEvidence.SourceType.MANUAL, source_name="A")
    record_analog_evidence(link, source_type=PartAnalogEvidence.SourceType.MANUAL, source_name="A")

    assert link.evidence.count() == 1


# --- PRICE / STOCK -------------------------------------------------------------------------


def test_analog_keeps_its_own_current_price_independent_of_original(db, original, analog):
    link_analog(original=original, analog=analog)

    prices = effective_part_customer_prices([original, analog])
    assert prices[original.pk] == Decimal("10000")
    assert prices[analog.pk] == Decimal("6000")


def test_missing_current_price_is_none_not_zero(db, original):
    priceless = create_manual_part(name="Без цены", article="NOPRICE-1")
    link_analog(original=original, analog=priceless)

    prices = effective_part_customer_prices([priceless])
    assert prices[priceless.pk] is None


def test_source_price_never_overwrites_the_current_customer_price(db, original, analog):
    before = effective_part_customer_prices([analog])[analog.pk]

    link_analog(
        original=original, analog=analog,
        source_type=PartAnalogEvidence.SourceType.SUPPLIER,
        source_price=Decimal("999999"),
        source_currency="USD",
    )

    after = effective_part_customer_prices([analog])[analog.pk]
    assert after == before == Decimal("6000")


def test_relation_creation_issues_no_stock_movement(db, original, analog):
    before = StockMovement.objects.count()

    link_analog(original=original, analog=analog)
    link = PartAnalog.objects.get(original=original, analog=analog)
    set_analog_verification(link, PartAnalog.VerificationState.VERIFIED)

    assert StockMovement.objects.count() == before


# --- IMPORT-LEVEL EVIDENCE CONFLICT AUDIT --------------------------------------------------


def test_conflicting_supersession_claims_are_reported_not_resolved(db, original):
    first_target = create_manual_part(name="Новый артикул 1", article="NEW-1")
    second_target = create_manual_part(name="Новый артикул 2", article="NEW-2")

    link_analog(
        original=original, analog=first_target,
        relation_type=PartAnalog.RelationType.SUPERSESSION,
        source_type=PartAnalogEvidence.SourceType.MANUFACTURER_CATALOG,
    )
    link_analog(
        original=original, analog=second_target,
        relation_type=PartAnalog.RelationType.SUPERSESSION,
        source_type=PartAnalogEvidence.SourceType.SUPPLIER,
    )

    report = audit_part_analogs()

    assert len(report.conflicting_supersessions) == 1
    conflict = report.conflicting_supersessions[0]
    assert conflict.original_id == original.pk
    assert set(conflict.targets) == {"Новый артикул 1", "Новый артикул 2"}
    # Both rows survive: neither claim is auto-deleted or silently picked.
    assert PartAnalog.objects.filter(
        original=original, relation_type=PartAnalog.RelationType.SUPERSESSION
    ).count() == 2


def test_multiple_analogs_for_one_original_is_not_a_conflict(db, original):
    """Unlike SUPERSESSION, many ANALOG targets for one original is normal."""
    first = create_manual_part(name="Аналог 1", article="A-1")
    second = create_manual_part(name="Аналог 2", article="A-2")
    link_analog(original=original, analog=first)
    link_analog(original=original, analog=second)

    report = audit_part_analogs()

    assert report.conflicting_supersessions == []


def test_audit_counts_verification_and_relation_type_breakdown(db, original, analog):
    link, _ = link_analog(original=original, analog=analog)
    set_analog_verification(link, PartAnalog.VerificationState.VERIFIED)

    report = audit_part_analogs()

    assert report.total_relations == 1
    assert report.by_verification[PartAnalog.VerificationState.VERIFIED] == 1
    assert report.by_relation_type[PartAnalog.RelationType.ANALOG] == 1


def test_audit_flags_evidence_with_price_but_no_currency(db, original, analog):
    link_analog(
        original=original, analog=analog,
        source_type=PartAnalogEvidence.SourceType.SUPPLIER,
        source_price=Decimal("100"),
    )

    report = audit_part_analogs()

    assert report.evidence_price_missing_currency == 1
    assert report.evidence_price_missing_observed_at == 1


def test_audit_public_eligible_matches_confirmed_links(db, original, analog):
    from apps.catalog.public_catalog import confirmed_links

    link, _ = link_analog(original=original, analog=analog)
    set_analog_verification(link, PartAnalog.VerificationState.VERIFIED)

    report = audit_part_analogs()

    assert report.public_eligible_relations == confirmed_links().count()


# --- SAFETY: relation creation touches nothing else ---------------------------------------


def test_manufacturer_identity_is_never_derived_from_the_relation(db, original, analog):
    link, _ = link_analog(original=original, analog=analog)
    set_analog_verification(link, PartAnalog.VerificationState.VERIFIED)

    original.refresh_from_db()
    analog.refresh_from_db()
    assert original.manufacturer.name == "BRP"
    assert analog.manufacturer.name == "PROX"


def test_customs_identity_is_independent_per_part(db, original, analog):
    PartCustomsInfo.objects.create(part_type=original, manufacturer="BRP")
    PartCustomsInfo.objects.create(part_type=analog, manufacturer="PROX")

    link_analog(original=original, analog=analog)

    assert PartCustomsInfo.objects.get(part_type=original).manufacturer == "BRP"
    assert PartCustomsInfo.objects.get(part_type=analog).manufacturer == "PROX"


def test_recommended_price_is_untouched_by_linking_or_verifying(db, original, analog):
    before = analog.recommended_price

    link, _ = link_analog(original=original, analog=analog)
    set_analog_verification(link, PartAnalog.VerificationState.VERIFIED)

    analog.refresh_from_db()
    assert analog.recommended_price == before


def test_photo_is_not_shared_between_original_and_analog(db, original, analog):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.core.images import add_image
    from tests.public_catalog_support import jpeg_bytes

    upload = SimpleUploadedFile("photo.jpg", jpeg_bytes(), content_type="image/jpeg")
    add_image(original.images, image=upload, caption="", by=None)
    link_analog(original=original, analog=analog)

    assert original.images.count() == 1
    assert analog.images.count() == 0


def test_historical_sale_and_repair_are_unchanged_by_relation_lifecycle(db, original, analog):
    sale = Sale.objects.create(
        status=Sale.Status.COMPLETED, customer_name="Иванов", revenue_total=Decimal("15000"),
    )
    repair = RepairOrder.objects.create(status=RepairOrder.Status.COMPLETED, customer_name="Петров")
    sale_revenue_before = sale.revenue_total
    repair_status_before = repair.status

    link, _ = link_analog(original=original, analog=analog)
    set_analog_verification(link, PartAnalog.VerificationState.VERIFIED)
    set_analog_verification(link, PartAnalog.VerificationState.REJECTED)

    sale.refresh_from_db()
    repair.refresh_from_db()
    assert sale.revenue_total == sale_revenue_before
    assert repair.status == repair_status_before


def test_sale_and_repair_models_have_no_analog_substitution_field(db):
    """Structural guarantee behind §15/§16: nothing can silently substitute a
    part on a document - there is no field for it to happen through."""
    sale_fields = {f.name for f in Sale._meta.get_fields()}
    repair_fields = {f.name for f in RepairOrder._meta.get_fields()}
    assert "analog" not in sale_fields
    assert "part_analog" not in sale_fields
    assert "analog" not in repair_fields
    assert "part_analog" not in repair_fields


# --- SECURITY ------------------------------------------------------------------------------


def test_analog_reject_requires_manage_parts_permission(client, make_user, original, analog):
    make_user("seller", role=roles.SELLER)
    client.login(username="seller", password=PASSWORD)
    link, _ = link_analog(original=original, analog=analog)

    from django.urls import reverse

    response = client.post(reverse("part_analog_reject", args=[link.pk]))

    assert response.status_code == 403
    link.refresh_from_db()
    assert link.verification_state == PartAnalog.VerificationState.UNVERIFIED


def test_analog_reject_works_for_authorized_manager(client, make_user, original, analog):
    make_user("manager", role=roles.MANAGER)
    client.login(username="manager", password=PASSWORD)
    link, _ = link_analog(original=original, analog=analog)

    from django.urls import reverse

    response = client.post(reverse("part_analog_reject", args=[link.pk]))

    assert response.status_code == 302
    link.refresh_from_db()
    assert link.verification_state == PartAnalog.VerificationState.REJECTED


def test_public_catalog_never_exposes_evidence_or_internal_note(
    public_client, original, analog
):
    from django.urls import reverse

    link, _ = link_analog(
        original=original, analog=analog, note="внутренняя заметка СЕКРЕТ",
        source_type=PartAnalogEvidence.SourceType.SUPPLIER,
        source_name="Секретный поставщик Х",
    )
    set_analog_verification(link, PartAnalog.VerificationState.VERIFIED)
    original.is_public = True
    original.save(update_fields=["is_public"])
    analog.is_public = True
    analog.save(update_fields=["is_public"])

    response = public_client.get(
        reverse("public_catalog_part", args=[original.public_id])
    )

    body = response.content.decode()
    assert "СЕКРЕТ" not in body
    assert "Секретный поставщик" not in body
