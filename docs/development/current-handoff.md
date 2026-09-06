# Current handoff

Task: customs catalog-backed auto-fill hotfix. Status: TRUE BLOCKER at full
catalog-source gate. Previous audit accepted by user; new instruction allows
ALL loaded catalogs and no longer limits source logic to 65bd691.
Branch: hotfix/customs-auto-fill-non-weight-fields. Started from 47ed377.
Runtime base/main/origin/main/live: 34a813bef1c03c6c22c480e87b9ad0261ead7251,
independently reverified after git fetch and over SSH.
Worktree: /Users/maxinik/Developer/DenStock-customs-hotfix.

Read [full catalog audit](../audits/customs-full-catalog-source-audit.md).
It supersedes the earlier claim that the two SM articles lack usable USD:
AftermarketCatalogPart supplies SM-01357=203.26 and SM-09374=127.21 USD.
Applied source is batch #6 DEALER 2026 although source code says dealer_2023.
Do not substitute older failed-upload prices or MSRP.

Completed: inventory of all actual DB/catalog/import models; exact and alias
coverage for all 84 rows; all sheets/rows of five unique production catalog
XLSX (six private copies plus two host legacy files); JSON presets; local test
fixtures; source provenance and conflicts; repeated live reconciliation.
Each DB audit used REPEATABLE READ READ ONLY.
Coverage: B/C/D/E/K/L/J 84 resolvable; A/F 0; existing M resolver 0.
M has one literal Ski-Doo description hint (SM-01357); remaining 83 no mapping.
No same-priority exact/price-source conflicts on current 84 rows.

Blockers: tracking has no shipment source for 84; country missing in catalog
fields/raw files for 84; application mappings absent for 83 plus one possible
literal Ski-Doo derivation. No fake tracking from SKU, no country from BRP brand,
no arbitrary application from category BRP/Aftermarket.

Live reconciliation: 117 canonical/115 effective/2 fully returned, 84 rows,
199.000 quantity, 697122.00 RUB, all deltas/missing/duplicates zero.
No implementation, candidate build, tests, backup or deploy was performed:
user explicitly prohibits deploy with ANY unresolved non-weight field.
Only this handoff and the new audit document changed. Production, main,
schema and business data remain unchanged. Runtime is NOT fixed.

Durable evidence: /Users/maxinik/Developer/DenStock-customs-evidence-20260906.
Scripts run via ssh root@185.250.44.206 and docker compose exec -T web
python manage.py shell; exact scripts and JSON results are in that directory.
Source inventory script, raw-file scanner, per-row coverage, all file hashes
and final customs_reconcile output are preserved. No source XLSX committed.

Next: obtain shipment/country source or explicit changed column/source contract;
obtain deterministic application mapping. Then implement resolved catalog
population, aftermarket dealer USD, field-conflict rejection, blank-safe weights
without changing canonical universe. Add all requested regressions and update
workflow docs. Required commands: pytest; ruff check .; djlint templates --check;
python manage.py check; python manage.py makemigrations --check. Generate fresh
production snapshot candidate, require every non-weight field populated and
all reconciliation deltas zero. Only after PASS perform user's signed PRE,
deploy, live reconciliation/XLSX, signed POST, business fingerprints and main FF.
