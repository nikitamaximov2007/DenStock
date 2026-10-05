# Oil request unit: Option C implementation design

Status: DESIGN READY, IMPLEMENTATION PENDING. Nothing below is implemented.
Owner decision: Option C. The customer keeps buying packages; each request line
records its unit and package volume explicitly, so the line has an unambiguous
physical quantity in liters. Revised after the independent Astra audit
(O1 confirmed, O2 conditionally true, O3 confirmed, O4 confirmed).

Quantity domains are already decided by `apps.catalog.quantity_units`:
PIECE (whole count) and MEASURED (0.001 precision: oil, and parts counted in
л/кг/м). Oil packages are a commercial unit on top of the MEASURED liters, not a
third domain.

Production evidence (Astra, read only): 0 oil PartTypes, 0 oil request rows,
0 fractional non-oil rows. Repeat the audit before any rollout; do not rely on it.

## 1. Fields

Three fields carry the meaning. A fourth, `base_quantity`, was evaluated and
removed (see 1.4).

### 1.1 `quantity_requested` (existing)

| Aspect | Decision |
|---|---|
| Django type | `DecimalField(max_digits=12, decimal_places=3)` (unchanged) |
| Null | no |
| Lifetime | immutable after creation (snapshot of the customer's choice) |
| DB constraints | existing `custreq_line_quantity_positive` (> 0) |
| Meaning | a count in `quantity_unit`: pieces, a measure, or packages |
| Validation | service: whole for `piece` and `oil_package`, 0.001 for `measured` |
| Migration | none |
| Pricing | `price_seen` is per `quantity_unit`; shown total = `quantity_requested x price_seen` |
| Request -> Sale | see section 4 |

### 1.2 `quantity_unit` (new)

| Aspect | Decision |
|---|---|
| Django type | `CharField(max_length=16, choices=RequestQuantityUnit)`: `piece`, `measured`, `oil_package` |
| Null | yes. NULL means the unit of a legacy row cannot be proven. Never set by guessing |
| Lifetime | immutable; written once by `create_customer_request` from the part, never from the client |
| DB constraints | `custreq_line_unit_known`: NULL or one of the three values |
| Migration | additive nullable column, no default; then the backfill command (section 2) |
| Pricing | defines what `price_seen` is per (piece, unit of measure, package) |
| Request -> Sale | selects the conversion in section 4 |

### 1.3 `oil_package_volume_l_snapshot` (new)

| Aspect | Decision |
|---|---|
| Django type | `DecimalField(max_digits=8, decimal_places=3)` (same as `PartType.oil_package_volume_l`) |
| Null | yes; required exactly for `oil_package` |
| Lifetime | immutable; copied from `PartType.oil_package_volume_l` at creation; later volume changes never rewrite it |
| DB constraints | `custreq_line_package_volume_iff_package`: `quantity_unit = 'oil_package'` requires a value > 0; any other unit (and NULL) requires NULL |
| Migration | additive nullable column; never backfilled (the historical volume was not recorded) |
| Pricing | `price_seen` for a package line is the package price; liter price = `price_seen / volume` |
| Request -> Sale | liters = `quantity_requested x oil_package_volume_l_snapshot` |

### 1.4 `base_quantity`: removed

`base_quantity` (liters for oil, the count otherwise) is fully determined by the
three immutable values above. Storing it would add a second copy that must stay
equal to `quantity_requested x volume`, an equality SQLite cannot check in a
constraint. A model property `base_quantity` computes it (and a query can
annotate the same expression), so integrity is preserved with one source of
truth. The earlier `quantity_unit_source` field is also dropped: NULL already
means "not proven", and no row is ever filled by assumption.

`unit_name` / `unit_short_name` stay display snapshots; for `oil_package` they
become "Упаковка" / "упак." at creation.

## 2. Migration and backfill

1. Schema migration (`customer_requests 00xx`): add the two nullable columns and
   the two constraints. Reversible, no data step, no table rewrite.
2. `python manage.py backfill_request_quantity_units`: dry run by default,
   `--apply` to write, idempotent, only rows with `quantity_unit IS NULL`, prints
   counts per bucket, never customer data. It never changes `quantity_requested`,
   `price_seen` or any other snapshot.

| Existing row | Decision | Why it is not a guess |
|---|---|---|
| Part is not oil, the line's own unit snapshot is л/кг/м | `measured` | read from the row's recorded unit |
| Part is not oil, any other unit snapshot | `piece` | read from the row's recorded unit |
| Part is oil (any source) | stays NULL, listed for manual review | public rows were packages, messenger repeat rows were liters, and the package volume at request time was never stored: the number cannot be proven |

3. If the dry run on production shows 0 rows left NULL (expected from the Astra
   evidence), a later migration makes `quantity_unit` NOT NULL. If any oil row is
   left, the column stays nullable and those rows keep the legacy behavior.

4. Close the remaining reclassification hole first: `PartType.has_stock_or_history`
   must also count customer request lines, so the oil flag cannot flip after a
   request exists (today only stock, movements, sales and repairs block it).

## 3. Old ambiguous rows

Rows left NULL are displayed with "единица не зафиксирована", their money total
as "цена уточняется" for oil, and Request -> Sale keeps today's manual oil step
(the operator enters liters; completion checks that an oil line is present). No
automatic conversion ever runs on them.

## 4. Request -> Sale

| `quantity_unit` | Draft line | Completion check |
|---|---|---|
| `piece` | `quantity_requested` pieces (today's flow) | equal count, whole |
| `measured` | `quantity_requested` of the measure (today's flow, fractions allowed) | equal quantity |
| `oil_package` | pre-filled `add_oil_volume_to_sale(liters)` with liters = `quantity_requested x snapshot`, split over lots, draft only | sale oil liters equal the request's liters |
| `oil_package`, volume changed since the request | no pre-fill; operator enters liters; both volumes shown | operator-confirmed liters |
| NULL (legacy) | today's manual oil step | oil line present |

Money is the existing rule: the sale is priced from the current package price per
liter (`add_oil_volume_to_sale`); `price_seen` stays a historical display.

## 5. Flows that create lines

* Catalog: the cart keeps whole packages; `set_line` and `create_customer_request`
  compare `packages x volume` with available liters (fixes O1). The message shows
  both ("доступно 4 упак. (20 л)"); the package count shown is how many full
  packages the liters hold, a display of capacity, never a stored or rounded
  quantity.
* Messenger repeat purchase: historical oil sale lines are liters; packages =
  liters / current volume, proposed only when exact; otherwise the line is
  unavailable with "уточните у менеджера" (fixes O3, no rounding).
* Web repeat purchase: the same rule replaces `math.ceil` for oil (fixes O4).
* All three write `quantity_unit` and, for oil, the volume snapshot.

## 6. Display

* piece `2 шт.`; measured `1,5 кг`; oil_package `2 упак. x 4 л = 8 л`, price
  `4 000 ₽ за упак.`; NULL `2` with "единица не зафиксирована" (fixes O2).
* Customer surfaces never show liters alone for a package line.
* One formatter in `quantity_units` takes the line's unit; `messaging.summary_line`
  and the operator console stop reading `PartType.unit` directly.

## 7. Package volume changes after creation

`PartType.oil_package_volume_l` cannot change once the part has history (existing
guard). If it changes on a part with only requests, the request keeps its
snapshot; Request -> Sale stops pre-filling (section 4).

## 8. Tests needed

1. Constraints: valid shapes accepted, each invalid shape refused, SQLite and
   PostgreSQL 16.
2. Catalog: 6 packages of 4 L against 20 L refused, 5 accepted; line stores
   `oil_package`, volume 4, "упак."; the client cannot choose the unit.
3. Service bypass: fractional packages and fractional pieces refused, fractional
   measure accepted.
4. Messenger and web repeat: 8 L sold -> 2 packages; 2.5 L -> unavailable; nothing
   ceiled; total = packages x current package price.
5. Request -> Sale for each row of the table in section 4, including the changed
   volume and the NULL legacy row.
6. Display strings on card, console, both bots and the cabinet.
7. Backfill: dry run writes nothing; apply fills only proven buckets; oil rows stay
   NULL; idempotent; snapshots untouched; no customer data printed.
8. Regression: piece and measured requests unchanged; oil sale and repair liters
   unchanged; `audit_piece_quantities` unchanged.

## 9. Rollout order

1. `has_stock_or_history` counts request lines (section 2.4). Deploy.
2. Schema migration and model fields, no behavior change. Deploy.
3. Write path: explicit unit on every new line; liter-based availability; display
   with NULL fallback. Deploy.
4. Production: read-only `audit_piece_quantities`, then the backfill dry run;
   owner reviews counts; `--apply`.
5. Request -> Sale oil pre-fill for `oil_package` rows.
6. NOT NULL migration only if no legacy NULL row remains.
