# Public Catalog Stage 1: shared domain contracts

Stage 1 provides read-only domain services for internal search and a future
Public Catalog. It does not publish parts, add routes, or change stock and
price workflows.

## Current customer price

`apps.catalog.public_contracts.resolve_current_customer_price(part)` reads only
the current authoritative `PartType.recommended_price`. A finite positive
`Decimal` returns `known`; an absent or unusable value returns `clarify`.
Existing catalog imports and pricing settings remain responsible for all price
calculation and refreshes. The facade never creates settings, recalculates a
price, refreshes a model, or writes data.

## Available total

`apps.inventory.availability.available_totals(part_ids)` delegates stock and
reservation semantics to `apps.inventory.movement.live_stock_rows()`, then
returns only `{part_id: Decimal}`. It includes physical bulk and serial stock,
excludes receiving and quarantine, and subtracts only active unexpired
reservations. Every requested part has a result, including `Decimal("0")`.
It does not use or rebuild the `StockBalance` cache and exposes no warehouse
location, lot, batch, serial, or reservation detail.

## Public part facts

`apps.catalog.public_contracts.build_public_part_facts(part_ids)` batches
public-safe facts in requested-ID order. A fact contains a server-side part ID,
canonical article, English name, confirmed nonblank Russian customs name,
canonical manufacturer display, unit data, price result, and available
quantity. Article and manufacturer are resolved through the existing canonical
inventory presentation helpers.

The DTO deliberately excludes location, lot, batch, serial, barcode, receipt,
supplier, purchase cost, minimum price, markup, FX, notes, customer data,
staff data, and movement data. Its methods are read-only and do not create or
mutate database records.
