# Codex handoff prompt template

Paste-ready prompt for Codex when it continues Claude's WIP checkpoint.
Fill in the placeholders (layer number, task description) before sending.
Protocol details: [ai-collaboration.md](ai-collaboration.md).

---

You are continuing work on DenisStock from an existing Claude Code WIP
checkpoint on branch `wip/layer-XX-claude-handoff`.

Task: <one-sentence task description>.

Important:

- Do not restart the task from scratch.
- Do not create a competing implementation.
- Do not delete Claude's WIP unless it is clearly broken and you can
  justify it in your report.
- Preserve project architecture (views orchestrate, services mutate).
- Preserve stock safety rules: stock changes only through existing
  receipt/inventory posting flows; scanning and drafts never change stock.
- Preserve backward compatibility (data, URLs, legacy address codes).

First inspect:

```
git status --short
git branch --show-current
git log --oneline --decorate --max-count=10
git diff --stat HEAD~1..HEAD
```

- `AGENTS.md` (project rules for AI agents)
- `docs/development/current-handoff.md` if it exists (the transfer file:
  what is done, what is not, exact next steps)
- relevant app files (`apps/<app>/models.py`, `services.py`, `views.py`)
- relevant tests (`tests/test_<app>.py`)

Then:

1. Summarize what Claude already changed.
2. Identify unfinished work.
3. Finish only the requested task. No scope creep.
4. Add/update tests for the changed behavior.
5. Update docs if user-facing behavior changed (user manual, ChatGPT
   context doc, operations doc).
6. Run:
   ```
   pytest
   ruff check .
   djlint templates --check
   python manage.py check
   python manage.py makemigrations --check
   ```
7. Commit with a clear message (no WIP prefix for the final commit).
8. Final report must include:
   - what changed
   - what was preserved from Claude's checkpoint
   - tests run and their results
   - risks
   - deploy commands

Hard rules:

- Do not touch secrets, `.env*`, dumps, backups, `*.xlsx` files, or `.claude/`.
- Do not silently rewrite historical posted documents or price snapshots.
- Do not mutate stock except via existing posting flows.
- Do not create duplicate apps/services if existing ones already solve
  the problem.
- Do not change production infrastructure (Docker, deploy scripts, env)
  unless explicitly requested.
- Money is `Decimal`; BRP customer RUB prices are whole rubles
  (ROUND_HALF_UP). New warehouse addresses use `S01-L02-D03-C08`
  (no zones, no K/X letters); legacy codes stay readable.
