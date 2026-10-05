# Quantity domain audit: pieces vs oil, packages vs litres

Base: `d2adede` (request channel routing). Candidate branch: `claude/request-quantity-domain`.
Executable proof: `tests/test_piece_quantity_invariant.py` (documents),
`tests/test_piece_stock_boundary.py` (stock intake and correction),
`tests/test_quantity_domain_writers.py` (domains, consumption, compensation,
admin) and `tests/test_piece_stock_boundary_postgresql.py` (PostgreSQL 16 forced
lock races and rollbacks). Revised after the independent Astra audit; its full
document was not available in this session, its findings were applied as given.

## 1. The authoritative quantity model

Correction after the Astra audit: `is_oil=False` does NOT mean "whole pieces".
Production has non-oil parts in л and кг (no stock history yet), and the owner's
rule is that fractions are legitimate for products measured by a physical
measure. The domain is decided once, in `apps/catalog/quantity_units.py`:

| Domain | Parts | Quantity |
|---|---|---|
| PIECE | not oil, unit шт / компл / упак, or any unit not classified as a measure | whole numbers |
| MEASURED | oil (always, liters), and parts whose unit is Литр/л, Килограмм/кг, Метр/м | 0.001 precision |

Oil is a subtype of MEASURED (liters of stock). Its PACKAGE is a separate
commercial concept used only by customer requests and prices (section 5 and
`docs/design/oil-request-unit.md`); it is not a quantity domain.

The existing unit model has no fractional semantics: `Unit` is a staff-editable
name and short name. The classifier therefore reads the seeded measured units by
name (`MEASURED_UNITS`); any other or new unit counts pieces until classified in
code, which is the safe default for stock. Two guards keep the classification
stable: a part with stock or history cannot change its unit across the PIECE /
MEASURED line (`PartType.clean`), and a unit used by such parts cannot be renamed
across it (`Unit.clean`). A sturdier classifier is a `Unit.quantity_kind` field;
that needs a migration and is proposed, not done.

| Use | PIECE | MEASURED (non-oil) | Oil |
|---|---|---|---|
| Stock, movements | pieces | measure, 0.001 | liters, 0.001 |
| Sale, repair, reservation, write-off lines | pieces | measure | liters (oil volume flows) |
| Public catalog cart | whole pieces | whole units of measure | whole PACKAGES |
| Customer request | pieces | measure | packages (catalog) or liters (repeat, O3) |

## 2. Entry-point matrix

"Before" is `d2adede`, proven by the classification run of the same tests.

| Entry point | Input unit | Validation before | Stored in | Non-oil fraction before | Oil | Conversion | Class (before) | After |
|---|---|---|---|---|---|---|---|---|
| Public cart `public_cart.parse_quantity` / `set_line` | int | int 1..99 | session | no | packages | none; compares packages with litres (O1) | VALID | unchanged |
| Request service `create_customer_request` (public form, reorder) | Decimal | > 0, 3 dp | `quantity_requested` | YES | packages / litres | none | VALIDATION GAP | refused, `CustomerRequestError` |
| Messenger repeat purchase `build_reorder_preview` | historical sold - returned | min(history, available) | via request service | YES (1.5 copied) | litres x package price (O3) | none | VALIDATION GAP | line unavailable, "уточните у менеджера" |
| Web cabinet repeat purchase `customer_accounts.reorder` | historical | `math.ceil` | cart int | silently 1.5 -> 2 | ceil(litres) as packages (O4) | ceil | VALIDATION GAP (silent rounding) | line state `fraction`, not added, shown as 1,5 |
| Request -> Sale `prepare_request_sale` | `quantity_requested` | none | `SaleLine.quantity` | YES | skipped, operator adds litres | none | VALIDATION GAP | refused by line name |
| `complete_request_sale` | draft lines | equality with request | completed Sale | YES (proven: completed) | presence only | none | VALIDATION GAP | refused |
| Manual sale add lot (`AddSaleLotForm`, `add_stock_lot_to_sale`) | Decimal | > 0 | `SaleLine.quantity` | YES | any lot listed | none | VALIDATION GAP | form + service refuse |
| Manual sale add oil (`add_oil_volume_to_sale`) | litres | > 0, oil only | `SaleLine.quantity` | n/a | litres | price per litre | VALID | unchanged |
| Reservation add lot (`AddLotForm`, `add_stock_lot_to_reservation`) | Decimal | > 0 | `ReservationLine.quantity` | YES | litres | none | VALIDATION GAP | form + service refuse |
| Reservation -> Sale `create_sale_from_reservation` | reservation line | none (copied) | `SaleLine.quantity` | derived | litres | none | LEGACY-ONLY after fix | `complete_sale` refuses |
| Repair add lot (`AddRepairLotForm`, `add_stock_lot_to_repair_order`) | Decimal | > 0 | `RepairIssueLine.quantity` | YES | any lot | none | VALIDATION GAP | form + service refuse |
| Repair add oil | litres | > 0 | `RepairIssueLine.quantity` | n/a | litres | price per litre | VALID | unchanged |
| Write-off add lot / quick write-off | Decimal | > 0 | `WriteOffLine.quantity` | YES | litres | none | VALIDATION GAP | form + service refuse |
| Quick Actions `perform_action`, cart `add_scan` / `set_row_quantity` | "1,5" accepted | > 0 | Sale / Reservation / Repair lines | YES | refused | none | VALIDATION GAP | refused, `ActionError` |
| Partial cancellation / return (`returns._add_line`) | Decimal | > 0, <= returnable | `StockReturnLine` | YES (0.5 of 2) | refused | none | VALIDATION GAP (feeds reorder) | refused, except the full remainder of a legacy line |
| `complete_sale` / `complete_repair_order` | draft | none | completed document | YES | litres | none | VALIDATION GAP | final gate refuses |
| Staff edit of a request line quantity | none | n/a | n/a | n/a | n/a | n/a | NOT REACHABLE (no such flow) | n/a |
| Stock intake and correction (receipts, batches, lots, adjustments, found stock, transfers, counts, recounts, counting sessions) | Decimal | > 0 | `StockLot` | YES | litres | none | VALIDATION GAP | closed, see section 7 |

Classification run on unmodified `d2adede`: 22 of 31 cases failed, every one a
"DID NOT RAISE" on a fractional piece, including a legacy 1.5-piece request
completing into a real Sale. Oil and whole-piece cases passed on both.

## 3. The rule now

`validate_part_quantity(quantity, part_type)` returns
"Для штучной детали количество должно быть целым." for a non-integral quantity
of a PIECE part and `None` otherwise (a whole number never needs the domain, so it
costs no query). It never rounds. Each service raises its own domain error
(`CustomerRequestError`, `SaleError`, `ReservationError`, `RepairError`,
`WriteOffError`, `ActionError`, `ReturnError`, `ReceiptError`, `StocktakingError`,
`SectionRecountError`, `CountingError`, `LandedCostError`, `InventoryError`),
which the views turn into a message; the lot forms mirror the rule in `clean()`.
No migration, no data change, oil untouched.

## 4. Legacy data (read only)

`python manage.py audit_piece_quantities` lists fractional PIECE rows (not oil,
unit not л/кг/м) in
sales, customer requests, repairs, reservations, write-offs, stock lots, receipt
lines, batch lines, transfers, inventory counts, section recount lines and stock
movements, by
table, row id, document id, status, part id, unit and quantity, with counts of
parts per unit for pieces, measured parts and oil. It prints no customer names
or phones and
changes nothing. Production was NOT checked from this session; the Astra
read-only production audit reported 0 fractional non-oil rows in every checked
table, 0 oil parts and 0 oil request rows. Repeat it immediately before
deployment. Equivalent SQL (PostgreSQL, read only; `m` excludes measured units):

```sql
WITH m AS (SELECT id FROM catalog_unit WHERE lower(rtrim(name, '.')) IN ('литр','л','килограмм','кг','метр','м') OR lower(rtrim(short_name, '.')) IN ('литр','л','килограмм','кг','метр','м')),
piece AS (SELECT id FROM catalog_parttype WHERE NOT is_oil AND unit_id NOT IN (SELECT id FROM m))
SELECT 'sales_saleline' AS t, count(*) FROM sales_saleline WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'customer_requests_customerrequestline', count(*) FROM customer_requests_customerrequestline WHERE part_type_id IN (SELECT id FROM piece) AND quantity_requested <> floor(quantity_requested)
UNION ALL SELECT 'repairs_repairissueline', count(*) FROM repairs_repairissueline WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'sales_reservationline', count(*) FROM sales_reservationline WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'writeoffs_writeoffline', count(*) FROM writeoffs_writeoffline WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'inventory_stocklot', count(*) FROM inventory_stocklot WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'inventory_stockmovement', count(*) FROM inventory_stockmovement WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'inventory_stocktransfer', count(*) FROM inventory_stocktransfer WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'receipts_receiptline', count(*) FROM receipts_receiptline WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'procurement_batchline', count(*) FROM procurement_batchline WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity)
UNION ALL SELECT 'stocktaking_inventorycountline', count(*) FROM stocktaking_inventorycountline WHERE part_type_id IN (SELECT id FROM piece) AND counted_quantity <> floor(counted_quantity)
UNION ALL SELECT 'stocktaking_sectionrecountline', count(*) FROM stocktaking_sectionrecountline WHERE part_type_id IN (SELECT id FROM piece) AND quantity <> floor(quantity);

SELECT u.short_name, p.is_oil, count(*) FROM catalog_parttype p JOIN catalog_unit u ON u.id = p.unit_id GROUP BY 1, 2 ORDER BY 2, 1;

SELECT r.source, count(*) FROM customer_requests_customerrequestline l
JOIN customer_requests_customerrequest r ON r.id = l.request_id
JOIN catalog_parttype p ON p.id = l.part_type_id WHERE p.is_oil GROUP BY 1;
```

Legacy rows stay as they are: shown exactly (1,5), never rounded; a draft or
request holding one cannot be completed; a completed legacy line can still be
cancelled or returned in full.

## 5. Oil: flows, defects, options

Probe: oil part, unit л, package 4 L at 4000 ₽, stock 20 L.

| Step | Unit of the number | Observed |
|---|---|---|
| Catalog card | price per package, stock in litres | "4000 ₽ за упаковку", "В наличии: 20 л" |
| Cart | packages | 6 packages (24 L) accepted against 20 L (O1); 21 refused with "Сейчас доступно 20 л" |
| Request service | packages | 6 accepted; the check compares 6 packages with 20 litres (O1) |
| Request line snapshot | number in packages, unit snapshot from `PartType.unit` | operator card prints "6 л × 4 000 ₽ = 24 000 ₽" for 6 packages (O2) |
| Request -> Sale | packages -> litres | not converted: the operator adds litres manually; completion only checks an oil line exists |
| Messenger repeat purchase | sold litres reused as the request number | 2.5 L sold at 1000 ₽/L previews 2.5 x 4000 = 10 000 ₽ (O3) |
| Web cabinet repeat purchase | sold litres ceiled into cart packages | 2.5 L -> 3 packages = 12 L (O4) |

Option A: packages stay the customer unit; fix the edges. Availability checks
compare `packages x oil_package_volume_l` with litres; cards label the number
"упак."; repeat purchase converts litres to packages by an explicit owner rule
or refuses the oil line; Request -> Sale stays manual. No migration. Old oil
request rows remain unlabelled numbers whose unit depends on their source.

Option B: litres everywhere. Catalog shows a price per litre and takes decimal
litres; request stores litres; Request -> Sale converts automatically. Changes
public catalog semantics (customers buy sealed packages), needs a migration of
existing package-count rows to litres, and prices by the litre.

Option C: packages for the customer, with an explicit unit on the request line.
Additive fields on `CustomerRequestLine` (unit kind and package volume
snapshot), every conversion through one function in `quantity_units`, Request ->
Sale proposes `packages x volume` litres for the operator to confirm. Requires an
additive migration and a backfill decision for existing oil rows by source.

Recommendation: Option C. Existing data is already mixed (public lines are
packages, messenger repeat lines are litres), so only an explicit unit on the
line removes the ambiguity, and it keeps the catalog in the packages customers
actually buy.

Owner decision (2026-10-04): Option C. The implementation design is in
`docs/design/oil-request-unit.md`; it is NOT implemented yet, so no oil row,
catalog semantics or Request -> Sale oil conversion was changed and O1 to O4
remain open.

## 6. Remaining gaps and risks

* The PIECE / MEASURED classifier reads seeded unit names. A unit staff create
  later (for example "Грамм") counts pieces until it is added to
  `MEASURED_UNITS`; a `Unit.quantity_kind` field would remove that dependency but
  needs a migration (proposed, not done).
* A legacy fractional PIECE lot blocks every write that would leave it
  fractional (sale, repair, write-off, found stock, transfer split or merge)
  until a count or recount brings it to a whole number. Production currently has
  none (Astra audit); repeat the audit before release.
* A whole compensation into a legacy fractional lot (for example cancelling a
  whole sale whose lot was later corrupted to 0.5) is refused: provenance only
  covers a fractional line restoring its own fraction. Reconcile the lot first.
* Customer-request oil defects O1 to O4 stay open until Option C is implemented.
* Admin document headers (status fields) remain editable; only the quantity
  paths (lines, lot identity) were closed here.
* Production has not been checked from this session.

## 7. Physical stock writers

Rule: no write may leave a PIECE lot fractional, and a quantity a write moves for
a PIECE part must be whole. Two exceptions, both explicit:

* B, reconciliation: `adjust_stock_lot_quantity` (manual adjustment, stocktaking
  apply, section recount apply, found stock) judges only the RESULTING balance,
  so a documented -0.5 that brings a legacy 1.5 to 1 is allowed.
* C, compensation: `return_stock_lot_quantity`, `restore_written_off_stock_lot_quantity`
  and `_consume_stock_lot` (return cancellation) accept a fractional quantity only
  with `compensates=<line>`: a persisted line of the same part that is itself
  fractional (and, for a return line, whose source sale or repair line is too),
  with the quantity not exceeding it. There is no bypass flag.

The guard (`_ensure_piece_stock`) sits at each mutation point; domain services
validate earlier and map `InventoryError` to their own error.

| Writer | Domain input | Service layer | Physical mutation point | Validation | Error the caller sees | HTTP behavior | Transaction | Legacy recovery | Test |
|---|---|---|---|---|---|---|---|---|---|
| Receipt draft add / edit | part's domain | `receipts.add_line`, `update_line` | none (draft) | `_validate_line_values` | `ReceiptError` | message | row | edit the line | boundary: receipt add/edit |
| Receipt posting | part's domain | `post_receipt` | `BatchLine` create, `finalize_cost`, `create_stock_lot`, `create_part_items` | all lines re-validated before the batch; `InventoryError`, `LandedCostError` mapped | `ReceiptError` | message | `post_receipt` atomic | fix the line, then post | boundary: legacy draft; writers: stock error mapped; PG: commits nothing |
| Procurement batch line | part's domain | `BatchLineForm` (ModelForm, admin) | none until costing | `BatchLine.clean`; `save()` alone does not validate, so costing and lot creation re-check | form error | form redisplay | row | edit in draft batch | boundary: batch line form |
| Batch finalization | part's domain | `procurement.finalize_cost` | landed cost on lines | every line before any write | `LandedCostError` | message | atomic | edit the line | boundary: batch not costed |
| Direct lot create / remainder / edit | part's domain | `create_stock_lot`, `update_stock_lot` | new or edited lot | moved quantity | `InventoryError` | message | atomic | a legacy batch remainder (0.5) cannot be stocked: it was never physical | boundary: lot create/edit |
| Transfer (split / merge) | part's domain | `perform_stock_transfer` | source lot down, target lot created or merged | quantity, every FIFO portion, source and target balances | `InventoryError` | message | atomic | refused until the legacy lot is reconciled; whole-lot move allowed | boundary: transfer; writers: merge into legacy |
| Whole-lot move | unchanged | `move_stock_lot` | location only | none needed (quantity unchanged) | `InventoryError` | message | atomic | moves a legacy lot as it is | boundary: legacy move |
| Manual adjustment | part's domain | `adjust_stock_lot_quantity` | lot quantity | resulting balance (B) | `InventoryError` | message | atomic | B: correct to whole | boundary: adjust; PG: two reconciliations |
| Stocktaking count / apply | part's domain | `update_counted_quantity`, `complete_inventory_count` | via adjustment | counted whole; adjustment guard | `StocktakingError` | message | atomic | count to whole (B) | boundary: count, legacy reconciled |
| Section / cell recount | part's domain | `set_section_line_quantity`, `allocate_section_line`, `apply_section_recount` | via adjustment and recount lots (created at 0) | line and allocation whole; adjustment guard | `SectionRecountError` (apply marks FAILED, full rollback) | message | atomic | recount to whole (B) | boundary: section recount input, apply reconciles a legacy lot |
| Counting session | warehouse part's domain (catalog lines: PIECE) | `set_line_quantity`, `convert_to_receipt`, `post_session` | via receipt posting | before any card or receipt; `ReceiptError` mapped | `CountingError` | message | atomic | fix the count | boundary: counting session |
| Found stock (single, scanner group) | part's domain; oil refused in group | `add_found_stock`, `post_found_stock_group` | via adjustment; first lot created at 0 | quantity; adjustment guard | `InventoryError` | message | atomic (group all or nothing) | blocked until reconciled | boundary: found; PG: group rollback releases lock |
| Sale consumption | part's domain | `complete_sale` (also Quick Actions, Request -> Sale, reservation -> sale) | `sell_stock_lot` -> `_consume_stock_lot` | line whole (document gate), legacy lot check before writes, guard | `SaleError` (`ActionError`, `CustomerRequestSaleError` upstream) | message | atomic | blocked until reconciled | writers: legacy lot; PG: sale after reconciliation |
| Repair consumption | part's domain | `complete_repair_order` | `issue_stock_lot` | same | `RepairError` | message | atomic | same | writers: legacy lot |
| Reservation activation | part's domain | `activate_reservation` | none (balance cache only) | every line whole | `ReservationError` | message | atomic | edit the draft | writers: reservation gate |
| Write-off | part's domain | `add_stock_lot_to_write_off`, `complete_write_off`, `quick_write_off` | `write_off_stock_lot_quantity` | line whole, legacy lot check, guard | `WriteOffError` | message | atomic | write-off of a legacy fraction goes through a count instead | writers: legacy lot |
| Write-off restore (cancellation) | historical line | `cancel_write_off` | `restore_written_off_stock_lot_quantity` | guard with `compensates=line` (C) | `WriteOffError` | message | atomic | exact restore of a legacy line | writers: legacy write-off cancel |
| Return, partial cancellation | historical line | `returns._add_line`, `complete_return`, `cancel_sale_line_quantity` | `return_stock_lot_quantity` | whole, or the full remainder of a legacy fractional line; guard with `compensates=line` | `ReturnError` / `SaleError` | message | atomic | exact restore | invariant: legacy line cancelled in full; writers: return and its cancellation |
| Return cancellation | historical line | `cancel_return` | `reverse_stock_return_lot` -> `_consume_stock_lot` | guard with `compensates=line` | `ReturnError` | message | atomic | exact reversal | writers: return cancellation |
| Whole sale / repair cancellation | historical lines | `cancel_sale`, `cancel_repair_order` | `return_stock_lot_quantity` | guard with `compensates=line`; now mapped | `SaleError` / `RepairError` | message | atomic | exact restore | writers: legacy sale and repair cancel; no provenance refused |
| Serial items | always 1 | `create_part_items`, status services | instance status | integer count by construction | n/a | n/a | atomic | n/a | existing suites |
| Admin | n/a | Django admin | document line inlines, lot / item identity, part unit, unit name | inlines read-only; lot and item `part_type`, `batch_line` read-only (quantity, status, location already were); `PartType.clean` and `Unit.clean` refuse a domain change with history | admin form error | form error | n/a | n/a | writers: admin |
| Management commands, imports | n/a | `seed_public_catalog_demo` (via `create_stock_lot`, whole), `import_preset` (catalog only), price backfills (price fields only), `backfill_opening_movements` / `rebuild_stock_balance` (read lots) | none new | through services | n/a | n/a | per command | n/a | n/a |
| Direct ORM / SQL | any | none | any | not guarded (`QuerySet.update` bypasses models); found only in tests | n/a | n/a | n/a | the audit finds the result | audit |

Proof runs:

* `tests/test_piece_stock_boundary.py` (26) against `71910f3`: 25 failed.
* `tests/test_quantity_domain_writers.py` against `b1b2106` (without the new domain
  API tests): 19 failed, 3 compensation tests passed (compensation already worked;
  they now guard that the new rule keeps it working).
* PostgreSQL 16 (`tests/test_piece_stock_boundary_postgresql.py`): every race is
  forced and fails unless `pg_stat_activity` shows the contender blocked on the lot
  lock. A fractional adjustment queued behind a valid one is refused on the fresh
  balance; two reconciliations of one legacy lot cannot both apply; a sale queued
  behind a reconciliation sells from the whole balance; a refused found-stock group
  rolls back its earlier +1 and releases its lock to a waiting adjustment; a receipt
  with one legacy line commits nothing. The fixtures create their units and number
  sequences, so the tests also pass with `--reuse-db`, where migration-seeded rows
  are gone: a version that seeded only units failed 2 of 5 there
  (`NumberSequence` missing) before reaching the race. Other PostgreSQL suites
  still rely on migration-seeded rows: run them with `--create-db`, because
  transactional tests flush those rows and `--reuse-db` cannot restore them.

## 8. AUD-01: lifetime receipt cap

Root cause: `create_stock_lot`, `update_stock_lot` and `remaining_qty` limited a
batch line by the SUM OF ITS LOTS' CURRENT QUANTITY. Selling 2 of 10 left 8 on
the shelf, so another 2 could be received against the same line (a second lot in
another cell, or "Лот на остаток").

Model: `BatchLine.quantity` is the expected quantity. Intake is a lot created
from the line (`create_stock_lot`, also used by receipt posting, which creates
its own batch line per posting) and received by `receive_stock_lot`, which
writes an immutable RECEIVE_LOT movement. Partial intake into several cells is
legitimate. There is no receipt reversal: a posted receipt cannot be cancelled,
a lot never returns to `receiving`, lots are never deleted. Serial items already
used a lifetime count (`existing_count`) and were not affected.

Invariant: `remaining = line.quantity - received_quantity(line)`. What each lot
took in is decided from ledger evidence only (`apps/inventory/lot_provenance.py`,
section 9); nothing is inferred from a lot merely having no movement.

Concurrency: every capacity check runs after `select_for_update` on the batch
line row (create: line after the target cell check; edit: lot, cells, line, as
documented in `update_stock_lot`), so a second intake waits and re-reads the
committed history. Forced PostgreSQL 16 races (contender seen blocked in
`pg_stat_activity`): two +2 on 8 of 10, exactly one passes; a pending lot edited
upward while another intake commits is refused; a sale committing while an
intake waits does not reopen capacity (this one fails on `c5847f2`).

## 9. Lot provenance: lots without RECEIVE_LOT

Astra's read-only production audit (954 lots, 199 without RECEIVE_LOT, 5 active
transfer lots without any movement) showed that "no movement" does not mean
"unrecorded receipt". The code history explains every case:

| Origin | Since | Own movements | Evidence used |
|---|---|---|---|
| Transfer target | 627a84b (2026-07-15) | none; its MOVE_LOT is recorded on the SOURCE lot | a `stock_transfer` MOVE_LOT of another lot of the same line, into this lot's original cell, for exactly `initial_quantity`, written in the same transaction (the note and `StockTransfer` row corroborate) |
| Return into a new cell | layer 18 | RETURN_LOT first | first own movement RETURN_LOT for exactly `initial_quantity`, same transaction |
| Section recount | 7bdcdd5 | ADJUST_IN `section_recount` first, opened at 0 | that first movement |
| Found stock (scanner group) | 877fd0b | ADJUST_IN `found_addition` first, opened at 0 | that first movement; it IS the intake of its own synthetic batch line |
| Pending receipt | always | none yet | status `receiving` |
| Received by status flip | before 108b5ad (2026-09-29) | no RECEIVE_LOT; later sales etc. are recorded | the ledger rebuilds the lot's starting quantity (own in/out, transfer portions out, transfer merges in) and it equals `initial_quantity` |
| Anything else (for example quantity edited without a movement before 108b5ad / 2c64484) | | | none: UNKNOWN, intake unproven, the line stays closed |

`backfill_opening_movements` used to write RECEIVE_LOT for every physical lot
without movements: transfer targets (recording moved stock as a new receipt),
pending lots (a second RECEIVE_LOT when they are received) and status-flipped
lots (a receipt dated today). It no longer touches lots at all. Lot provenance is
read from the ledger by `received_quantity` and reported by the read-only
`python manage.py audit_lot_provenance` (class totals, active and movement-less
counts, closed lines, lines with more proven intake than their quantity, and per
lot evidence; no customer data). Do not run the old backfill on production: the
deployed code still has the old behavior.

Defects of 0ba1416 found by this review (proven by tests run against it): a
found-stock batch line left its whole quantity receivable again (duplicate
intake); a status-flipped legacy lot closed its line; the backfill wrote three
false receipts.
