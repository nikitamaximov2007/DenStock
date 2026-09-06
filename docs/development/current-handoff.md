# Current handoff

Task: customs catalog-backed auto-fill hotfix. Status: BLOCKED on three
product decisions (tracking, SPI country, application area). BRP country
fallback is implemented, tested and committed.
Branch: hotfix/customs-auto-fill-non-weight-fields. HEAD: 86419f5.
Runtime base/main/origin/main/live: 34a813bef1c03c6c22c480e87b9ad0261ead7251,
reverified over SSH 2026-09-06 (production HEAD matches, all containers up).
Worktree: /Users/maxinik/Developer/DenStock-customs-hotfix.

## Done in this takeover (Kimi, after Codex)

- Preserved Codex's uncommitted BRP=КАНАДА fallback as commit 86419f5
  (services.py, new tests/test_customs_brp_country_fallback.py, three docs).
  No changes were discarded; nothing was force-pushed.
- 13 new fallback tests pass; ruff, djlint, manage.py check,
  makemigrations --check, pip check, git diff --check all clean.
  tests/test_clients_overview_sorting.py::test_date_is_the_last_customer_facing_document
  fails identically on clean main 34a813b: pre-existing, unrelated.
- Template semantics re-verified from
  apps/actions/customs_template/supplier_order_template.xlsx:
  row 7 marks B/C/D/E/J «ОБЯЗАТЕЛЬНО» but NOT column A. Since the first
  exporter 2c43365 column A is written as None for manual entry.
  Template's own example row uses M=СНЕГОХОД for a BRP part.
- Live production read-only verification (SPI): Manufacturer SPI (id 19)
  country='', zero manufacturers in the whole DB have a country.
  AftermarketCatalogPart has no country field at all.
  SM-01357 = dealer_cost_usd 203.26, SM-09374 = 127.21 (preserved).
- Live production read-only verification (application): PartCompatibility
  empty; VehicleMakes: Ski-Doo/Lynx/Yamaha=Снегоход, Can-Am=Квадроцикл,
  Sea-Doo=Гидроцикл. No article-to-vehicle mapping exists.

## Exact blockers (product decisions needed, smallest list)

1. TRACKING (column A, 84/84 blank). No shipment/parcel entity exists
   anywhere in the system; the template itself does not mark A mandatory.
   Decision needed: (a) drop A from the auto-fill contract and keep it
   manual, or (b) name a deterministic source. No tracking number may be
   invented.
2. SPI COUNTRY (2 rows: SM-01357, SM-09374). No country in any loaded
   catalog, file, or manufacturer record; the approved КАНАДА rule covers
   BRP only. Decision needed: explicit country value for these two SPI
   aftermarket parts, or accept 2 blank F cells.
3. APPLICATION AREA (column M, 84/84 blank). PartCompatibility is empty;
   category is the catalog source group, not vehicle type. The template
   example uses СНЕГОХОД; one SM row (SM-01357) has a literal SKI DOO
   description hint. Decision needed: approve a universal business value
   (e.g. СНЕГОХОД) or another deterministic rule.

Do NOT deploy until all three are decided by the user. After decisions:
implement population, add regressions, run full gate, fresh production
snapshot gate (all non-weight blanks = 0, all reconciliation deltas zero),
then the signed PRE/deploy/live/POST workflow from the original task.

Historical universe (34a813b) is load-bearing and untouched:
117 canonical / 115 effective / 2 fully returned, 84 XLSX rows,
199.000 qty, 697122.00 RUB, all deltas zero (customs_reconcile).
Never return the movement-based exporter or the 201-unit bug.

Evidence: /Users/maxinik/Developer/DenStock-customs-evidence-20260906.
