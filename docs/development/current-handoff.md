# Current handoff

Task: Customs non-weight auto-fill hotfix. Status: TRUE BLOCKER at forensic gate.
Classification: hotfix, no runtime edits.
Branch: `hotfix/customs-auto-fill-non-weight-fields`.
Base/current runtime: `34a813bef1c03c6c22c480e87b9ad0261ead7251`.
Worktree: `/Users/maxinik/Developer/DenStock-customs-hotfix`.

Independently verified actual origin/main and live production release.
Live reconciliation: 117 source lines, 84 aggregate rows, 199.000 units,
697122.00 RUB, all deltas and missing/duplicate line counts zero.

Read [forensic report](../audits/customs-auto-fill-hotfix-blocker.md).
Previous population `65bd691` replayed on the same 84 current canonical rows.
Blockers: tracking 84 blanks (always manual), application 84 blanks
(saved values empty and compatibility yields none), USD 2 blanks
(SM-01357, SM-09374 lack BRP/Polaris links supported by previous exporter).
Do not invent values, restore legacy defaults, or deploy an incomplete candidate.

Changed files: this handoff and forensic report only.
No main reset, DB writes, migrations, runtime changes, or deployment.
Production has pre-existing untracked docker-compose.signing.yml; untouched.

Local evidence: /tmp/denstock-customs-forensics/{current,previous}.xlsx and
audit.json. Reproduction: /tmp/denstock-customs-audit-builder.py reads
/tmp/denstock-customs-old-services.py (git show 65bd691:apps/actions/services.py),
builds a READ ONLY repeatable-read Django script, executes it over SSH,
and saves both XLSX locally. No production files are written by that script.

Completed commands: git status/branch/log, git fetch origin, git worktree add,
SSH production git status/rev-parse and web DENSTOCK_APP_COMMIT,
live customs_reconcile --json, Git-history population/template inspection,
same-input XLSX replay and all-column blank/populated counts.

Next: resolve the three documented data/contract blockers, port population only,
preserve canonical universe and existing snapshots, implement blank-safe weight
formula and UI warning, add requested regressions, update user/operations docs.
Then pytest; ruff check .; djlint templates --check; python manage.py check;
python manage.py makemigrations --check. Run fresh production snapshot gate.
Only after PASS follow user's signed PRE backup/deploy/live checks/POST backup
and final fast-forward workflow. No deploy command is currently authorized by
the conditional gate because non-weight blanks remain.
