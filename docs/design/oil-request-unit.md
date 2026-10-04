# Oil request unit: Option C implementation design

Status: DESIGN READY, IMPLEMENTATION PENDING. Nothing below is implemented.
Owner decision (2026-10-04): Option C. The customer keeps buying packages; every
request line records its unit explicitly, snapshots the package volume, and so
carries an unambiguous physical quantity in litres.

Defects this closes (see `docs/audits/piece-quantity-domain-audit.md`, section 5):
O1 packages compared with litres in cart and request availability; O2 operator
card prints "6 л" for 6 packages; O3 messenger repeat bills litres at the
package price; O4 web repeat ceils litres into packages.

## 1. Fields on `CustomerRequestLine`

All additive and nullable, so the schema migration is safe on a live table and
old rows stay valid until the backfill decides them.

| Field | Type | Null | Meaning |
|---|---|---|---|
| `quantity_unit` | `CharField(max_length=16, choices=QuantityUnit)` | yes | What `quantity_requested` counts. `piece`, `oil_package`, `oil_liter`. NULL = recorded before explicit units. |
| `package_volume_l_snapshot` | `DecimalField(max_digits=8, decimal_places=3)` | yes | Litres in one package at request time (copy of `PartType.oil_package_volume_l`). Only for `oil_package`. |
| `base_quantity_l` | `DecimalField(max_digits=12, decimal_places=3)` | yes | Physical litres this line represents. `oil_package`: `quantity_requested x package_volume_l_snapshot`; `oil_liter`: `quantity_requested`; `piece`: NULL. |
| `quantity_unit_source` | `CharField(max_length=32, blank=True, default="")` | no | Provenance: `explicit` (written at creation), `backfill_piece`, `backfill_catalog_package_current_volume`, `backfill_repeat_liters`. Empty only on rows not yet decided. |

`QuantityUnit` lives in `apps/catalog/quantity_units.py` next to
`validate_part_quantity`, so the unit decision stays in one module.

Existing fields keep their meaning:

* `quantity_requested` stays the number the customer chose, in `quantity_unit`.
* `price_seen` stays the customer price per `quantity_unit` at creation (the
  package price for `oil_package`). Informational, never a contract, never
  rewritten.
* `unit_name` / `unit_short_name` become the display snapshot of
  `quantity_unit` ("Упаковка"/"упак." for `oil_package`), not of `PartType.unit`.

## 2. Constraints

Expressed as `CheckConstraint`s, tested on SQLite and PostgreSQL 16:

1. `custreq_line_unit_known`: `quantity_unit IS NULL OR quantity_unit IN ('piece','oil_package','oil_liter')`.
2. `custreq_line_package_volume_iff_package`: `oil_package` has
   `package_volume_l_snapshot > 0`; every other unit (and NULL) has it NULL.
3. `custreq_line_base_liters_iff_oil`: `oil_package` / `oil_liter` have
   `base_quantity_l > 0`; `piece` and NULL have it NULL.
4. `custreq_line_unit_source_matches`: `quantity_unit IS NULL` iff
   `quantity_unit_source = ''`.

Whole-number rules (`piece` and `oil_package` quantities are integers,
`base_quantity_l = quantity x volume` exactly) are enforced in the service by the
shared validator, not in SQL: an integer check needs `FLOOR` in a CHECK
constraint, which SQLite only provides through a function Django registers per
connection. A follow-up migration may add a PostgreSQL-only `RunSQL` check
after the backfill.

A later migration, once the backfill leaves no NULL, makes `quantity_unit` NOT NULL.

## 3. Migration and backfill

1. Schema migration `customer_requests 00xx_request_line_quantity_unit`: add the
   four fields (nullable / empty default) and constraints 1 to 4. No data step,
   no lock-heavy rewrite. Reversible.
2. Backfill is a management command, not a migration data step, so the owner
   sees the counts before anything is written:
   `python manage.py backfill_request_quantity_units` (dry run by default,
   `--apply` to write, idempotent, only touches rows with `quantity_unit IS NULL`,
   prints counts per bucket, no customer data).

Buckets for old rows:

| Old row | Decision | Written |
|---|---|---|
| Part not oil | unambiguous | `piece`, `backfill_piece` |
| Oil, request source `public_catalog` | the number was packages (V1 contract) | `oil_package`, volume = CURRENT `oil_package_volume_l`, `base_quantity_l` = qty x volume, `backfill_catalog_package_current_volume` |
| Oil, request source `messenger_repeat` | the number was litres copied from a sale | `oil_liter`, `base_quantity_l` = qty, `backfill_repeat_liters` |
| Oil part without a package volume (should not exist: model constraint) | cannot decide | left NULL, reported |

## 4. Old ambiguous rows

* The package volume at request time was never stored. A backfilled
  `oil_package` row uses today's volume and says so through
  `quantity_unit_source`; staff screens show "объём упаковки взят из текущей
  карточки" next to it.
* A backfilled `oil_liter` row (messenger repeat) has a `price_seen` that is a
  package price recorded against litres (O3). It is not rewritten; its money
  total is shown as "цена уточняется" instead of `qty x price_seen`.
* Rows left NULL display as today, with "единица не зафиксирована".
* Completed, cancelled and anonymised requests are backfilled the same way:
  the command never changes `quantity_requested`, `price_seen` or any other
  snapshot, only adds the unit facts.

## 5. Flows

### Catalog creation

* The cart keeps whole packages (`public_cart.parse_quantity`, unchanged).
* `set_line` and `create_customer_request` compare `packages x volume` with
  available litres (fixes O1). The message names both: "доступно 4 упак. (20 л)",
  where the package count shown is how many FULL packages the litres hold; it
  is a display of capacity, never a stored or rounded request quantity.
* `create_customer_request` decides the unit server-side from the part, never
  from the client: `piece` for non-oil, `oil_package` for oil, and writes
  `package_volume_l_snapshot`, `base_quantity_l`, `quantity_unit_source=explicit`
  and the unit display snapshot. `oil_package` quantities must be whole.

### Messenger repeat purchase

* Historical oil sale lines are litres. Packages = litres / CURRENT package
  volume. Only an exact whole result is proposed; otherwise the line is
  unavailable with "уточните у менеджера" (no rounding, same rule as fractional
  pieces). The request line is `oil_package` at the current package price.

### Web repeat purchase

* Same conversion replaces `math.ceil` for oil in `customer_accounts.reorder`;
  the cart receives whole packages or the line becomes a `fraction`-style state
  with the same note (fixes O4).

### Request -> Sale

* `oil_package` / `oil_liter` rows with an explicit unit: `prepare_request_sale`
  pre-fills the draft with `add_oil_volume_to_sale(lot, base_quantity_l)` (draft
  only, no stock change), splitting over lots like pieces.
* If `package_volume_l_snapshot` differs from the part's current volume, the oil
  line is NOT pre-filled; the operator enters litres as today and sees both
  volumes.
* `_validate_request_sale_lines` compares oil litres like piece counts for
  explicit rows: the customer agreed to N packages, a different volume is a
  different deal and needs the request changed first. NULL-unit rows keep
  today's rule (an oil line must be present).
* Money stays the current rule: the sale is priced from the current package
  price per litre (`add_oil_volume_to_sale`), `price_seen` stays historical.

## 6. Display

Staff (request card, operator console, Telegram/MAX operator card, sale-from-
request draft):

* piece: `2 шт.`
* oil_package: `2 упак. x 4 л = 8 л`, price `4 000 ₽ за упак.`
* oil_liter (backfilled repeat): `2,5 л`, price "уточняется"
* NULL: `2` plus "единица не зафиксирована"

Customer (catalog, cart, request confirmation, bot summary, cabinet history):
`2 упак. по 4 л`, total from `price_seen x packages`; never litres alone for a
package line.

All formatting goes through `quantity_units` (`format_quantity` gains a unit
argument), so `messaging.summary_line` and the console stop reading
`PartType.unit` directly.

## 7. Package volume changes after creation

The request keeps `package_volume_l_snapshot` and `base_quantity_l`; changing
`PartType.oil_package_volume_l` never rewrites them. Sale pre-fill stops (see
section 5) and staff see "было 4 л, сейчас 5 л". Catalog shows the new volume
for new requests only.

## 8. Tests needed

1. Schema: constraints 1 to 4 accept the four valid shapes and reject each
   invalid one, on SQLite and PostgreSQL 16.
2. Catalog: 6 packages of 4 L against 20 L is refused; 5 accepted; line stores
   `oil_package`, volume 4, base 20 L, `explicit`, unit snapshot "упак.".
3. Request service bypassing forms: fractional packages refused; a client
   cannot choose the unit.
4. Messenger repeat: 8 L sold, volume 4 -> 2 packages; 2.5 L -> unavailable with
   reason; total = 2 x current package price.
5. Web repeat: same two cases; nothing is ceiled.
6. Request -> Sale: pre-filled 8 L priced from the current package price;
   volume changed after creation -> no pre-fill; edited volume -> completion
   refused; NULL-unit rows behave exactly as today.
7. Display: card, console, both bots and the cabinet show the strings in
   section 6 (no "6 л" for packages).
8. Backfill command: dry run writes nothing; apply fills each bucket, is
   idempotent, never changes `quantity_requested` / `price_seen`, prints no
   customer data.
9. Regression: piece requests unchanged; oil sale/repair litres unchanged;
   `audit_piece_quantities` unchanged.

## 9. Rollout order

1. Schema migration plus model fields, with no behavior change. Deploy and verify.
2. Write path: explicit units on every new line (catalog, messenger repeat, web
   repeat), litre-based availability, display with NULL fallback. Deploy.
3. On production: `backfill_request_quantity_units` dry run, owner reviews the
   counts, then `--apply`.
4. Request -> Sale oil pre-fill and litre equality for explicit rows.
5. Once no NULL remains: migration making `quantity_unit` NOT NULL (and the
   optional PostgreSQL integer check).
