# Repair Kanban: audit and implementation plan

Status: architecture audit complete, implementation intentionally blocked by
business-state mapping decisions.

Base: `8f5c9bac8880f46d784d2e68477da6ca5c922566`.

## 1. Current domain

`apps.repairs` currently models a warehouse issue document, not a complete
workshop job lifecycle.

`RepairOrder.status` has three values:

- `draft`: positions can be added or removed, stock is unchanged;
- `completed`: `complete_repair_order()` has issued every line through
  `apps.inventory.services.issue_part_item()` or `issue_stock_lot()`;
- `canceled`: a draft was canceled before stock changed.

The existing `completed` value means "parts were issued/installed". It does not
prove that diagnostics, customer approval, repair work, readiness or handover
of the vehicle happened. Reusing this field for Kanban would couple a visual
drag to a stock mutation and would destroy the historical meaning of existing
documents.

The existing model already provides useful card data:

- order number;
- customer and phone as stored text;
- vehicle type, make, model and identifier;
- problem description and comment;
- creator and timestamps;
- frozen cost of issued parts after warehouse completion.

It does not provide:

- workshop workflow status;
- assigned technician;
- due date;
- customer-facing repair total or labor price;
- workshop status transition history;
- optimistic concurrency version for workflow changes.

There is no existing repair audit/history model. `updated_at` is not sufficient
for a durable transition journal or an ABA-safe concurrency check. Existing
returns and stock movements describe parts, not workshop status changes.

## 2. Reusable architecture

The Kanban must remain inside `apps.repairs`; no second repairs app or stock
service is needed.

Reuse unchanged:

- `RepairOrder` as the card identity and customer/vehicle source;
- `RepairIssueLine` and frozen `cost_total` for issued-part information;
- `can_manage_repairs` for server-side mutation permission;
- `can_view_purchase_cost` for visibility of part cost;
- existing list/detail/create URLs and all issue/return services;
- standard Django CSRF middleware and partial-navigation shell;
- `transaction.atomic()` and `select_for_update()` patterns used elsewhere.

The board query can be one `RepairOrder` query with `select_related()` for
vehicle type, creator and assignee. It does not need to load issue lines for the
minimum card, so a growing board does not create N+1 queries.

## 3. Required separation

Recommended additive model fields on `RepairOrder`:

```text
workflow_status   nullable choice field, indexed
assigned_to       nullable User FK with SET_NULL
due_date          nullable DateField, indexed
workflow_version  positive integer, default 0
```

Recommended separate model:

```text
RepairWorkflowTransition
  repair_order    FK with PROTECT
  from_status     nullable choice value
  to_status       choice value
  actor           nullable User FK with SET_NULL
  version         positive integer
  created_at      indexed timestamp
```

`RepairOrder.status` must not be renamed, overloaded or updated by a Kanban
transition. `complete_repair_order()` remains the only repair service that
issues stock. Moving a card must never call inventory, receiving, sale, return,
write-off or recount services.

The seven requested workshop statuses can then be represented independently:

1. `new` - Новый.
2. `diagnostics` - Диагностика.
3. `approval` - Согласование.
4. `in_repair` - В ремонте.
5. `waiting_parts` - Ожидание запчастей.
6. `ready` - Готов.
7. `issued` - Выдан.

## 4. Legacy data blocker

An automatic mapping of existing rows is not safe:

- `draft` usually resembles "Новый", but a draft can already be in diagnostics
  or waiting for parts;
- `completed` only proves a parts issue and can represent work in progress,
  ready work or an already handed-over vehicle;
- `canceled` has no requested Kanban column and should remain canceled.

Recommended safe migration:

- keep `workflow_status=NULL` for every existing row;
- set `workflow_status=new` only for orders created after the migration;
- show a temporary read-only "Не распределено" column only when legacy rows
  with `NULL` exist;
- require an explicit user transition before a legacy row enters the seven
  canonical columns;
- never infer `issued` from warehouse `completed`.

This avoids a false production backfill. If the product owner instead provides
an explicit mapping rule, implement it as a separate dry-run/apply management
command, not in the schema migration.

## 5. Transition service

Add one service and keep views as orchestrators:

```text
transition_repair_workflow(
  order,
  *,
  target_status,
  expected_version,
  by,
) -> RepairOrder
```

Inside one transaction it must:

1. lock the `RepairOrder` row with `select_for_update()`;
2. reject canceled warehouse documents;
3. compare `expected_version` with `workflow_version`;
4. validate the target against the allowed transition graph;
5. return without a write when source and target are equal;
6. increment `workflow_version`;
7. save only workflow fields;
8. create exactly one `RepairWorkflowTransition` row;
9. return the new status, label and version.

A stale version must return HTTP 409. Invalid transitions return HTTP 400.
Permission denial remains HTTP 403. Any error rolls back both status and
history.

Recommended conservative graph, pending product confirmation:

```text
unclassified -> new
new -> diagnostics
diagnostics -> new | approval | waiting_parts
approval -> diagnostics | in_repair | waiting_parts
in_repair -> approval | waiting_parts | ready
waiting_parts -> diagnostics | approval | in_repair
ready -> in_repair | issued
issued -> no transitions
```

The exact graph is a business rule and must be approved before implementation.
It must not be left as "any status to any status", because the requested test
contract explicitly includes invalid transitions.

## 6. HTTP and UI plan

Additive URLs:

```text
GET  /repairs/board/                         repair_board
POST /repairs/orders/<pk>/workflow/          repair_workflow_transition
```

Keep every existing URL unchanged. The repairs sidebar entry can point to the
board after acceptance; local tabs should expose both "Доска" and "Список" so
`/repairs/orders/` remains directly reachable.

Board filters:

- assignee: active user id or unassigned;
- client: case-insensitive substring;
- workflow status;
- overdue: due date before today, excluding ready and issued;
- search across number, customer, phone, vehicle and problem text.

Card content:

- number linked to the existing detail page;
- customer;
- composed vehicle description;
- short problem description;
- assignee or "Не назначен";
- due date and overdue indicator;
- workflow status;
- waiting-parts indicator;
- frozen issued-part cost only under `can_view_purchase_cost`, labeled
  "Себестоимость деталей", never presented as a customer repair total.

Desktop drag and drop may update the card optimistically. On any non-2xx
response it must restore the original column, order and counts, then show the
server message. A select control on each editable card is required for mobile,
keyboard users and browsers where HTML drag events are unreliable.

The POST uses Django CSRF, sends `target_status` and `expected_version`, and
updates the card version from the response. The server never trusts a column
id, role hint, current status or version supplied only by JavaScript.

The existing repair detail should show workshop status, assignee, due date and
transition history separately from the warehouse document status. This naming
prevents "Проведён" and "Выдан" from looking like the same operation.

## 7. Creation and metadata

Extend `RepairOrderForm` and `create_repair_order()` with optional `assigned_to`
and `due_date`. New orders start in `new` with `workflow_version=0`. The
assignee queryset contains active users only.

Changing assignee or due date should use a small atomic service and permission
check. It must not be bundled into the drag endpoint and must not edit a
canceled order. Whether these metadata changes need their own history entries
is a product decision; status transition history alone does not audit them.

## 8. Amount blocker

The current domain has no labor price, invoice total or customer-facing repair
price. It stores only `cost_total`, the frozen internal cost of parts issued
from stock. Therefore the requested card "sum, if known" cannot safely be
labeled as repair sum.

Recommended first release behavior:

- show `cost_total` only as "Себестоимость деталей";
- preserve the existing `can_view_purchase_cost` gate;
- do not invent a total, use catalog prices or add labor pricing in this task.

A true repair total requires a separate product specification and financial
snapshot model.

## 9. Test plan

Model and migration:

1. Seven workflow choices and nullable legacy state.
2. New order starts in `new`; existing migration rows remain unclassified.
3. Assignee deletion uses `SET_NULL`; transition actor deletion keeps history.
4. No stock, balance, movement, line or document-status field changes during a
   workflow transition.

Service:

5. Every allowed edge succeeds and creates one history row.
6. Every disallowed edge raises and creates no history.
7. Same-status repeat is idempotent.
8. Canceled order cannot transition.
9. Stale `expected_version` is rejected.
10. Two concurrent PostgreSQL transactions from the same version produce one
    success and one conflict, with one history row.
11. Injected failure after status save rolls back status, version and history.
12. Existing `complete_repair_order()` still issues stock exactly once and does
    not silently mark the workshop job ready or issued.
13. Moving to `issued` creates no `StockMovement` and changes no quantities.

HTTP and permissions:

14. Anonymous board access redirects to login.
15. Authenticated read-only role can view but cannot mutate.
16. `can_manage_repairs` can perform a valid transition.
17. Direct POST cannot bypass invalid-transition validation.
18. Missing/invalid CSRF is rejected.
19. Stale version returns 409 with a safe message and current card state.

Rendering and filters:

20. Board renders canonical columns in order and conditionally renders the
    unclassified legacy column.
21. Cards map to exactly one column.
22. Assignee, client, status, overdue and search filters work together.
23. Canceled warehouse documents do not appear on the active board.
24. Cost is visible only with `can_view_purchase_cost`.
25. Query count remains constant as card count grows.
26. Empty board state is clear.

Browser:

27. Valid desktop drag persists after refresh.
28. Invalid drag rolls the card back.
29. Simulated 409 rolls the optimistic move back.
30. Mobile horizontal scroll keeps every column reachable.
31. Mobile select changes status without drag.
32. Keyboard selection and focus state work.
33. Back/Forward and partial navigation preserve the direct board URL.
34. Browser console has no errors.

Regression:

35. Existing repair, return, movement, reservation and cost tests remain green.
36. Completed orders remain immutable in their warehouse lines.
37. Repair returns and customs net repair consumption remain unchanged.
38. `makemigrations --check` is clean after the intentional additive migration.

## 10. Acceptance decisions required

Implementation should begin only after the following are approved:

1. Legacy rows remain "Не распределено", or an explicit dry-run mapping rule is
   supplied.
2. The proposed transition graph, especially backward transitions and whether
   `issued` is final.
3. `issued` means physical vehicle handover and never warehouse issue.
4. The first release shows only protected part cost, not a repair total.
5. Whether assignee/due-date edits require a separate audit history.

Until then, the safe state is this audit and plan with no Kanban schema, view,
JavaScript or data migration committed.
