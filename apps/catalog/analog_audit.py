"""Read-only audit of the part-analog relation/evidence system.

Bulk queries only, no N+1: every count below is one fixed-shape query
(``.count()``, a ``GROUP BY`` via ``.values().annotate()``, or a bounded
``.values_list()`` fetch) regardless of how many relations exist. See
``audit_part_analogs`` for the exact query list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count, Q

from .models import PartAnalog, PartAnalogEvidence

MAX_EXAMPLES = 200


@dataclass(frozen=True)
class ConflictRow:
    """One original with more than one distinct SUPERSESSION target."""

    original_id: int
    original_name: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class MultiTypeRow:
    """One (original, analog) pair recorded under more than one relation type."""

    original_id: int
    analog_id: int
    relation_types: tuple[str, ...]


@dataclass(frozen=True)
class AnalogAuditReport:
    total_relations: int
    by_verification: dict[str, int]
    by_relation_type: dict[str, int]
    by_source_type: dict[str, int]
    relations_without_evidence: int
    relations_with_inactive_target: int
    duplicate_relations: int
    multi_type_pairs: list[MultiTypeRow] = field(default_factory=list)
    conflicting_supersessions: list[ConflictRow] = field(default_factory=list)
    public_eligible_relations: int = 0
    evidence_price_missing_currency: int = 0
    evidence_price_missing_observed_at: int = 0


def audit_part_analogs() -> AnalogAuditReport:
    """Classify every PartAnalog relation and its evidence. Read-only."""
    total_relations = PartAnalog.objects.count()

    by_verification = dict(
        PartAnalog.objects.values_list("verification_state")
        .annotate(n=Count("id"))
        .values_list("verification_state", "n")
    )
    by_relation_type = dict(
        PartAnalog.objects.values_list("relation_type")
        .annotate(n=Count("id"))
        .values_list("relation_type", "n")
    )
    by_source_type = dict(
        PartAnalogEvidence.objects.values_list("source_type")
        .annotate(n=Count("id"))
        .values_list("source_type", "n")
    )
    relations_without_evidence = PartAnalog.objects.filter(evidence__isnull=True).count()

    relations_with_inactive_target = PartAnalog.objects.filter(
        Q(original__is_active=False) | Q(analog__is_active=False)
    ).count()

    # Constraint-backed since the (original, analog, relation_type) unique
    # constraint (see PartAnalog.Meta) - expected to always read 0. Kept as a
    # defensive integrity check, not a load-bearing dedup mechanism.
    duplicate_groups = (
        PartAnalog.objects.values("original_id", "analog_id", "relation_type")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    duplicate_relations = duplicate_groups.count()

    # Not a conflict by itself (§21): the SAME pair recorded under several
    # relation types is a legitimate distinct set of facts. Reported as
    # informational context alongside true conflicts below.
    multi_type_groups = list(
        PartAnalog.objects.values("original_id", "analog_id")
        .annotate(types=Count("relation_type", distinct=True))
        .filter(types__gt=1)
        .order_by("original_id", "analog_id")[:MAX_EXAMPLES]
    )
    multi_type_pairs = []
    if multi_type_groups:
        pairs = [(row["original_id"], row["analog_id"]) for row in multi_type_groups]
        type_lookup: dict[tuple[int, int], set[str]] = {}
        for original_id, analog_id, relation_type in PartAnalog.objects.filter(
            original_id__in={p[0] for p in pairs}, analog_id__in={p[1] for p in pairs}
        ).values_list("original_id", "analog_id", "relation_type"):
            type_lookup.setdefault((original_id, analog_id), set()).add(relation_type)
        for original_id, analog_id in pairs:
            types = type_lookup.get((original_id, analog_id), set())
            if len(types) > 1:
                multi_type_pairs.append(
                    MultiTypeRow(
                        original_id=original_id, analog_id=analog_id,
                        relation_types=tuple(sorted(types)),
                    )
                )

    # A real conflict (§21): the SAME original superseded into more than one
    # DIFFERENT target. Never auto-resolved - only reported for review.
    conflict_groups = list(
        PartAnalog.objects.filter(relation_type=PartAnalog.RelationType.SUPERSESSION)
        .values("original_id")
        .annotate(targets=Count("analog_id", distinct=True))
        .filter(targets__gt=1)
        .order_by("original_id")[:MAX_EXAMPLES]
    )
    conflicting_supersessions = []
    if conflict_groups:
        original_ids = [row["original_id"] for row in conflict_groups]
        names = dict(
            PartAnalog.objects.filter(original_id__in=original_ids)
            .values_list("original_id", "original__name")
            .distinct()
        )
        targets_by_original: dict[int, list[str]] = {}
        for original_id, analog_name in (
            PartAnalog.objects.filter(
                relation_type=PartAnalog.RelationType.SUPERSESSION,
                original_id__in=original_ids,
            )
            .values_list("original_id", "analog__name")
            .order_by("analog__name")
        ):
            targets_by_original.setdefault(original_id, []).append(analog_name)
        for original_id in original_ids:
            conflicting_supersessions.append(
                ConflictRow(
                    original_id=original_id,
                    original_name=names.get(original_id, ""),
                    targets=tuple(targets_by_original.get(original_id, ())),
                )
            )

    # Same rule public_catalog.confirmed_links() uses - a direct cross-check
    # that the audit's notion of "public eligible" matches production reality.
    public_eligible_relations = PartAnalog.objects.filter(
        verification_state=PartAnalog.VerificationState.VERIFIED,
        original__is_public=True,
        original__is_active=True,
        analog__is_public=True,
        analog__is_active=True,
    ).count()

    priced_evidence = PartAnalogEvidence.objects.filter(source_price__isnull=False)
    evidence_price_missing_currency = priced_evidence.filter(source_currency="").count()
    evidence_price_missing_observed_at = priced_evidence.filter(
        source_price_observed_at__isnull=True
    ).count()

    return AnalogAuditReport(
        total_relations=total_relations,
        by_verification=by_verification,
        by_relation_type=by_relation_type,
        by_source_type=by_source_type,
        relations_without_evidence=relations_without_evidence,
        relations_with_inactive_target=relations_with_inactive_target,
        duplicate_relations=duplicate_relations,
        multi_type_pairs=multi_type_pairs,
        conflicting_supersessions=conflicting_supersessions,
        public_eligible_relations=public_eligible_relations,
        evidence_price_missing_currency=evidence_price_missing_currency,
        evidence_price_missing_observed_at=evidence_price_missing_observed_at,
    )
