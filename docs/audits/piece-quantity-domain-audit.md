# Quantity domain audit: pieces vs oil, packages vs litres

Base: `d2adede` (request channel routing). Candidate branch: `claude/request-quantity-domain`.
Executable proof: `tests/test_piece_quantity_invariant.py` (SQLite and PostgreSQL 16).

## 1. The authoritative quantity model

`apps/catalog/quantity_units.py` decides what a quantity number means, from
`PartType.is_oil` only. Every quantity column is `Decimal(12, 3)`.

| Use | Ordinary part (`is_oil=False`) | Oil (`is_oil=True`) |
|---|---|---|
| Storage (`StockLot.quantity`, movements) | pieces | litres, 0.001 L |
| Staff sale (`SaleLine.quantity`) | pieces | litres (`add_oil_volume_to_sale`, price = package price / package volume, snapshots frozen) |
| Repair (`RepairIssueLine.quantity`) | pieces | litres (`add_oil_volume_to_repair_order`) |
| Reservation (`ReservationLine.quantity`) | pieces | litres |
| Write-off (`WriteOffLine.quantity`) | pieces | litres |
| Public catalog cart | whole pieces 1..99 | whole PACKAGES 1..99 (price shown "за упаковку") |
| Customer request (`CustomerRequestLine.quantity_requested`) | pieces | PACKAGES from the public catalog; LITRES from the messenger repeat purchase (see O3) |
| Package | none | `oil_package_volume_l` `Decimal(8, 3)` litres, mandatory iff oil (`parttype_oil_package_volume_required_iff_oil`); `recommended_price` is the package price. No package count, no pack multiplicity. |

Units seeded by `catalog 0002`: шт, компл, м, кг, л, упак. No code gives м or кг
fractional semantics; the only fractional domain in code is oil.

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
| Receipt (BULK), transfer, stocktaking count | Decimal | > 0 | `StockLot` | YES | litres | none | VALIDATION GAP, not changed | see section 6 |

Classification run on unmodified `d2adede`: 22 of 31 cases failed, every one a
"DID NOT RAISE" on a fractional piece, including a legacy 1.5-piece request
completing into a real Sale. Oil and whole-piece cases passed on both.

## 3. The rule now

`validate_part_quantity(quantity, part_type)` returns
"Для штучной детали количество должно быть целым." for a non-integral quantity
of a non-oil part, `None` otherwise. It never rounds. Each service raises its own
domain error (`CustomerRequestError`, `SaleError`, `ReservationError`,
`RepairError`, `WriteOffError`, `ActionError`, `ReturnError`), which the
existing views already turn into a message; the lot forms mirror the rule in
`clean()` (`clean_lot_form_quantity`) and the views show that text instead of the
generic one. No migration, no data change, oil untouched.

## 4. Legacy data (read only)

`python manage.py audit_piece_quantities` lists non-oil fractional rows in
sales, customer requests, repairs, reservations, write-offs and stock lots, by
table, row id, document id, status, part id, unit and quantity, with counts of
parts per unit for pieces and oil. It prints no customer names or phones and
changes nothing. Production was NOT checked from this session. Equivalent SQL
(PostgreSQL, read only):

```sql
SELECT 'sales_saleline' AS t, count(*) FROM sales_saleline l JOIN catalog_parttype p ON p.id = l.part_type_id WHERE NOT p.is_oil AND l.quantity <> floor(l.quantity)
UNION ALL SELECT 'customer_requests_customerrequestline', count(*) FROM customer_requests_customerrequestline l JOIN catalog_parttype p ON p.id = l.part_type_id WHERE NOT p.is_oil AND l.quantity_requested <> floor(l.quantity_requested)
UNION ALL SELECT 'repairs_repairissueline', count(*) FROM repairs_repairissueline l JOIN catalog_parttype p ON p.id = l.part_type_id WHERE NOT p.is_oil AND l.quantity <> floor(l.quantity)
UNION ALL SELECT 'sales_reservationline', count(*) FROM sales_reservationline l JOIN catalog_parttype p ON p.id = l.part_type_id WHERE NOT p.is_oil AND l.quantity <> floor(l.quantity)
UNION ALL SELECT 'writeoffs_writeoffline', count(*) FROM writeoffs_writeoffline l JOIN catalog_parttype p ON p.id = l.part_type_id WHERE NOT p.is_oil AND l.quantity <> floor(l.quantity)
UNION ALL SELECT 'inventory_stocklot', count(*) FROM inventory_stocklot l JOIN catalog_parttype p ON p.id = l.part_type_id WHERE NOT p.is_oil AND l.quantity <> floor(l.quantity);

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

**OWNER DECISION REQUIRED: OIL REQUEST UNIT.** Until then no oil row, catalog
semantics or Request -> Sale oil conversion was changed; O1 to O4 remain open.

## 6. Remaining gaps and risks

* Receipts (BULK non-oil), transfers and stocktaking counts still accept
  fractional piece quantities and can create fractional lots. Not changed here:
  stocktaking is the correction tool for legacy lots.
* A legacy fractional lot can make an integer FIFO split (Quick Actions, quick
  write-off, Request -> Sale) produce a fractional portion; that is refused
  explicitly, and the lot needs a stocktaking correction.
* Any non-oil part sold by м or кг with fractions is now refused. Run
  `audit_piece_quantities` (parts per unit) before release.
* Production has not been checked from this session.
