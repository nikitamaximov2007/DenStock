# Realtime request workspace

The internal customer-request workspace uses short-interval polling and the
durable `WorkspaceEvent` cursor log. Domain writes create a compact event in
the same database transaction; the authenticated endpoint returns at most 100
events after a validated cursor. The browser advances the cursor only after a
successful response and catches up when a hidden tab becomes active.

Customer-request display numbers are allocated by a locked singleton counter,
backfilled in deterministic creation order, and never derived from
`MAX(human_number)`. Opaque UUIDs and messenger identities remain the secure
identifiers for links, callbacks, and ownership checks.

Cancelled-request deletion is centralized in the request service. It locks and
rechecks status, clears active routing pointers, and relies on the existing
request-scoped cascade graph. The event log retains a deletion event so other
open workspaces can remove the row without an exception.
