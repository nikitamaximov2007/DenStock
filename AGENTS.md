# AGENTS.md: rules for AI agents working on DenisStock

This file is read by every AI agent (Claude Code, Codex, or any other) before
touching this repository. It exists so that two agents can work on the same
codebase without architectural drift, duplicated implementations, broken
migrations, or unsafe production changes.

Companion documents:

- [docs/development/ai-collaboration.md](docs/development/ai-collaboration.md):
  the full Claude + Codex handoff and parallel-work protocol (Russian).
- [docs/development/codex-handoff-template.md](docs/development/codex-handoff-template.md):
  paste-ready prompt for Codex when it takes over Claude's WIP.
- [docs/development/claude-handoff-template.md](docs/development/claude-handoff-template.md):
  how Claude prepares work so Codex can continue it.
- [docs/development/current-handoff.md](docs/development/current-handoff.md):
  the ACTIVE transfer file for the task in progress (created per task,
  may be absent when no handoff is active).

## Project

- Product name: **DenisStock** (visible name). Technical identifiers stay
  `denstock` / `DENSTOCK_*`: do not rename modules, env vars, or containers.
- Stack: Django 5.2, PostgreSQL in production, SQLite for dev/tests,
  pytest-django, ruff (line length 100), djlint.
- Production server path: `/opt/denstock` (Docker Compose).
- `main` must stay stable: only tested, deployable code. WIP goes to
  `wip/*` or feature branches (see the collaboration protocol).
- Never commit: secrets, `.env*` files, backups, database dumps, media
  dumps, `*.xlsx`/`*.xls` price files, `rclone.conf`, or `.claude/`.
  These are gitignored on purpose; do not force-add them.

## General rules

- Read the existing code before changing anything. The relevant app,
  its services, tests, and docs come first; assumptions come never.
- Do not rewrite architecture from scratch unless explicitly requested.
- Prefer small, focused changes. Do not touch unrelated files.
- Keep backward compatibility (data, URLs, management commands, tests).
- Views are orchestrators; business mutations live in each app's
  `services.py`. Follow that split for new code.

## Stock safety rules (non-negotiable)

- Stock is mutated ONLY through the existing receipt/inventory posting
  flows (`apps.receipts.services.post_receipt` and the flows built on it).
  Scanning, drafts, imports, and catalog operations never change balances.
- Do not silently rewrite historical posted documents or price snapshots.
- Use `Decimal` for money, never float.
- BRP customer RUB prices are WHOLE rubles: `retail_USD * rate *
  (1 + markup/100)`, quantized with `ROUND_HALF_UP` (see `apps/brp/pricing.py`).
  USD sources, rate, and markup are not rounded.

## Warehouse addresses

- Default format: `S01-L02-D03-C08` (no zones). Letters: S = shelving
  unit, L = level counted bottom-up, D = drawer, B = box/bin, C = cell.
- Do not use K, X, or zone prefixes for NEW addresses.
- Legacy codes (with zones or K/X letters) must remain readable and
  searchable; never migrate or delete existing `StorageLocation` codes.
- Single source of truth: `apps/warehouse/addresses.py` (`compose_address`).

## Before starting any task

Run and read, in this order:

```
git status --short
git branch --show-current
git log --oneline --decorate --max-count=5
```

Then read the relevant docs (`docs/`, this file,
`docs/development/current-handoff.md` if present) and the existing
implementation. Classify the task before writing code: new layer,
hotfix, docs-only, or deployment-only. The classification decides how
much testing and documentation the task needs.

## During work

- Do not create competing apps/modules if one already exists
  (check `config/settings/base.py` LOCAL_APPS and `config/urls.py` first).
- Do not duplicate migrations unnecessarily; never edit applied migrations.
- Write tests for every behavior change (`tests/test_*.py`, existing
  fixture patterns).
- Update docs when the user-facing workflow changes: the user manual,
  the ChatGPT context doc, and the relevant operations doc.
- UI language is Russian; calm premium B2B look; no em-dashes in
  templates or docs (enforced by tests).

## Before finishing

All of these must pass:

```
pytest
ruff check .
djlint templates --check
python manage.py check
python manage.py makemigrations --check
```

Finish with a final report: changed files, tests run, known risks, and
deploy commands. If the work is unfinished, write the handoff file
instead (see the collaboration protocol) and say so explicitly: never
present unfinished work as done.
