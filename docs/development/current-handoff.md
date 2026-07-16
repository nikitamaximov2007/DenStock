# Current handoff

- Task: автономный quality pass для поиска, ремонта, цен и навигации.
- Branch: `feature/overnight-quality-pass`.
- Base: `508a2bc47b66afd1adb075eb01beb8da0f102c0e`.
- Worktree: `F:\DenStock-overnight-quality`.
- Classification: cross-app feature and safety hardening, local only.

## Commits

1. `9d4b22ee89db612211218a095b60a449e216fd27` - canonical lookup and scanner safety.
2. `2c772aea0441d75b28bf3d35130b284895d08a0d` - repair returns and safe cancel.
3. `a523d2425f108fe534952ecb4bc3989f9c94b61d` - fractional pricing precision.
4. `d6bdd671c1034d8b43229d1d2028b155dd55221e` - locale-safe decimal inputs.
5. `c3a9c5d2af756ce5d39855e17a63ec1acf27595a` - persistent sidebar navigation.

## Implemented contract

- `apps.core.part_lookup` is the canonical resolver. Priority is exact,
  barcode, alias, then name. Replacement and superseded matches never replace
  exact identity; ambiguity remains explicit.
- Live availability comes from current `StockLot` and `PartItem` rows. Scanner,
  search, BRP/Polaris, receiving, actions and counting use the shared resolver.
- Scanner mutation flows have durable request tokens in actions migration 0008
  and counting migration 0004.
- Repair completion and return completion are idempotent. Partial returns are
  limited by issued minus completed returns.
- Customs quantity is ordinary usage plus nonnegative net repair usage. Active
  repair returns subtract from repair issues, canceled actions do not count.
- Completed returns can be canceled only through compensating inventory
  services with locks and conflict checks. Returns migration 0004 is additive.
- Price settings preserve Decimal(10,4) for rate and Decimal(6,2) for markups.
  Dot and comma work in every browser locale; preview uses BigInt, not float.
- Sidebar GET navigation progressively replaces only `#content`, preserves
  scroll, updates title, URL and active state, and supports Back/Forward.
  Any incompatible response falls back to a full page load. POST, logout,
  exports, downloads, external links and modified clicks are not intercepted.

## Verification

- Full pytest: 1274 passed, 0 failed, exit 0, 350.1 seconds.
- Stage A relevant regression profile: 432 passed.
- Stage B repair/returns/customs profile: 294 passed.
- Stage C pricing/valuation profile: 92 passed; final pricing profile includes
  locale and significant-zero regressions.
- Stage D UI profile: 223 passed after updating the relocated guard contract;
  focused returns/navigation/scan profile: 91 passed.
- Dedicated N+1 profile: 8 passed. Lookup is capped at 18 queries for eight
  results; counting, lot/search, repair/return and customs report query growth
  is bounded; warehouse valuation stays within 8 queries.
- Browser smoke: sidebar scrollTop stayed 695.2 through navigation and Back;
  topbar DOM state persisted; URL, title and aria-current updated; decimal
  preview returned BRP 5325; browser console had no warnings or errors.
- `ruff check .`: passed.
- `djlint templates --check`: passed, 95 files checked.
- `python manage.py check`: passed.
- `python manage.py makemigrations --check --dry-run`: no changes detected.
- `git diff --check`: passed.
- Node syntax checks passed for all changed JavaScript files.

## Manual acceptance

- Repeat the documented browser checklist with production-like data and roles.
- Confirm real scanner hardware cadence on receiving, movement and actions.
- Download one CSV and one customs Excel through the browser.
- Review the three additive migrations before deployment.

No merge, push, SSH, VPS, production database access or deploy was performed.
Production deployment commands are intentionally omitted until review and
explicit deployment approval.
