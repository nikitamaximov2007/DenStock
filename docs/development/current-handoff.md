# Current handoff

Task: customs catalog-backed auto-fill plus RU-name/style hotfix. Status:
RU content/style gate PASS; deployment remains blocked by approved manual
tracking/application fields and SPI country rows.
Branch: hotfix/customs-auto-fill-non-weight-fields.
Worktree: /Users/maxinik/Developer/DenStock-customs-hotfix.

## What shipped in this branch

- 86419f5 BRP=КАНАДА country fallback (preserved Codex work).
- bf49308 export test aligned with the approved fallback.
- Catalog auto-fill for empty historical fields (this takeover):
  EN name / manufacturer / USD resolve from the loaded supplier catalogs by
  card link or exact normalized article (BRP -> Polaris -> aftermarket);
  RU name derives from EN via the phrase-first deterministic translator; aftermarket
  USD is dealer_cost_usd. Saved operator versions always win; versions are
  never rewritten. Approved manual blanks stay blank: tracking, weights,
  application area, country of non-BRP brands.

## Qualification

- Full pytest: green except pre-existing failures that reproduce identically
  on clean 34a813b (test_clients_overview_sorting file, one partial repair
  cancellation test, one ai_support renderer test) - unrelated, untouched.
- ruff / djlint / manage.py check / makemigrations --check / git diff --check:
  clean.
- Snapshot gate: fresh production backup 2026-09-06_14-22-37 restored into
  isolated local PostgreSQL 16. Candidate XLSX: 84 rows, 199.000 qty,
 697122.00 RUB report match, all reconciliation deltas zero. Blank counts:
  B/C/D/E/J/K = 0; F = 2 (SM-01357, SM-09374, approved); A/G/H/M = approved
  manual. SM-01357 = 203.26 USD, SM-09374 = 127.21 USD preserved.

- RU candidate audit: 84/84 C cells populated; untranslated product words = 0,
  underscore identifiers = 0, mixed RU/EN garbage = 0 under the explicit
  brand/model/code allowlist. All C data cells use one Arial 12pt wrapped
  non-shrinking style; long rows use height 30 without reducing font size.

## Deploy state

PRE backup: backups/2026-09-06_14-22-37 (business_generation 10414,
business_sha256 fa6f5aa1...). Production was 34a813b at takeover.
Historical universe contract: 117 canonical lines, 84 rows, 199.000,
697122.00, deltas zero. Never return the movement-based exporter or the
201-unit bug.
