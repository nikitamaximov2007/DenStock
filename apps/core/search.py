"""Backward-compatible entry point for canonical warehouse part lookup."""

from .part_lookup import PartLookupCandidate, resolve_part_lookup

MIN_QUERY_LEN = 2

# Existing views/tests import PartSearchRow. The canonical candidate now carries
# the same presentation fields plus exact match metadata and live cell rows.
PartSearchRow = PartLookupCandidate


def search_parts(query: str) -> list[PartLookupCandidate]:
    value = str(query or "").strip()
    if len(value) < MIN_QUERY_LEN:
        return []
    return resolve_part_lookup(
        value,
        allow_partial=True,
        allow_name=True,
        allow_alias=True,
        include_price=True,
    ).candidates
