# Claude handoff template

How Claude Code prepares its work so Codex (or a fresh Claude session) can
continue it after a usage-limit cutoff. Protocol details:
[ai-collaboration.md](ai-collaboration.md).

## At task start

- Create or update `docs/development/current-handoff.md`: task name,
  branch, goal, planned files.
- Work on a `wip/*` or feature branch when the task is non-trivial;
  keep `main` stable.
- Keep the implementation incremental: models, then services, then views,
  then templates, then tests. Each stage should leave the repo in a state
  another agent can understand.

## During work

- Update `current-handoff.md` after major milestones (models done,
  services done, tests written).
- Make small commits when a coherent part is done: a committed checkpoint
  survives a limit cutoff, uncommitted work may not.
- When close to the usage limit: STOP. Write the handoff summary instead
  of starting risky changes (migrations, large refactors, deletions).

## Before limit / before handing off

```
git status --short
git diff --stat
```

Update `current-handoff.md` with:

- completed
- unfinished
- next steps
- risky areas
- commands run

Then commit WIP on a `wip/*` or `feature/*` branch if possible:

```
git switch -c wip/layer-XX-claude-handoff   # if not already on one
git add -A
git commit -m "WIP Layer XX from Claude before Codex handoff"
```

## Claude handoff summary template

Copy into `current-handoff.md` and fill in:

```markdown
# Current handoff

- Task:
- Branch:
- Current commit:
- Completed:
- Unfinished:
- Files changed:
- Tests run:
- Tests not run:
- Known risks:
- Next exact steps for Codex:
- Do not touch:
```

The "Do not touch" line lists files or behaviors the next agent must leave
alone (for example: applied migrations, posted documents, legacy address
codes, production env files).
