# DenisStock Agent Bootstrap

Read this file first on a new machine. Then read:

1. `docs/operations/new-machine-bootstrap.md`;
2. `docs/operations/disaster-recovery.md`.

Assume the old laptop is unavailable. Work from a fresh GitHub clone only.
Prepare development mode with the documented bootstrap script and stop at
`READY` or a real external prerequisite failure. Do not touch production,
production databases, production backups, or production secrets unless the
user explicitly authorizes a separate production task.

Never ask the user to paste a secret into chat. Development creates its own
local secret and SQLite database. Full data recovery needs the separate DR kit
and independent Yandex read-only credential; it is not a development setup
step.
