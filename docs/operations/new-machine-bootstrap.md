# DenisStock New Machine Bootstrap

## Goal

Turn a new Windows laptop with GitHub access into a safe DenisStock development
machine. This procedure uses only the Git repository and creates no production
connection or business data.

## What GitHub contains

Source code, migrations, tests, templates, Docker definitions, development
dependencies, and operation documentation.

## What GitHub intentionally does not contain

Production `.env`, database dumps, media, Yandex credentials, SSH keys,
signing private keys, real customer data, and backups.

Docker definitions include `docker-compose.signing.yml`, the overlay that mounts
the production signing directory. The overlay is tracked because it holds no key
material. The directory it mounts, `/etc/denstock/manifest-signing`, is a
production runtime asset and stays outside Git. Development needs neither.

## Required software

Development requires Git and Python 3.12 or later. Docker Desktop is optional
for normal SQLite development, but needed for local PostgreSQL/Docker testing.
Node/npm and rclone are not development prerequisites. rclone is needed only
for a full disaster-recovery drill.

## Clone repository

```powershell
git clone https://github.com/nikitamaximov2007/DenStock.git $HOME\source\DenStock
Set-Location $HOME\source\DenStock
```

## Verify repository

Read `AGENT-BOOTSTRAP.md`, `AGENTS.md`, and this guide. Confirm `git status
--short` is empty. Do not use a copied old working tree as source.

## Prepare local environment

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap-new-machine.ps1
```

The script checks Git and Python, creates `.venv`, installs the project with
development tools, generates `.env` only when absent, applies local SQLite
migrations, and runs Django checks. It never elevates privileges or modifies
Windows security policy persistently.

## Safe local configuration

The generated `.env` contains `config.settings.dev`, a locally generated
`DJANGO_SECRET_KEY`, `DENSTOCK_MODE=development`, and no `DATABASE_URL`.
SQLite stays under the clone and has no production data.

## Start DenisStock

```powershell
.\.venv\Scripts\Activate.ps1
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Create local test users only when needed for
development; they are not production users.

## Run checks

```powershell
pytest
ruff check .
python manage.py check
python manage.py makemigrations --check --dry-run
djlint templates --check
```

## Development-ready acceptance

The virtual environment is present, `.env` is local development-only,
`python manage.py check` passes, and the local server starts with SQLite.

## Restore real data with DR

Do not use the bootstrap script for real data. Follow
`docs/operations/disaster-recovery.md` with the independently stored DR kit
and a separate read-only Yandex Object Storage credential. The signed backup
manifest selects the exact commit and data; `main` is not a fallback.

## Secrets

Keep secrets in the password manager or the local environment. Pass recovery
credentials through environment variables only. Do not paste them into chat,
commit them, or add them to shared documentation.

## Never commit these files

`.env`, `.env.*` except tracked examples, `db.sqlite3`, `mediafiles/`,
`backups/`, private keys, credentials, dumps, and real catalog spreadsheets.

## Troubleshooting

If Git, Python, or Docker (when requested) is missing, install it using its
official installer and rerun the bootstrap script. If `python` resolves below
3.12, install Python 3.12+ and ensure it is on `PATH`. The script refuses to
replace an existing `.env`; inspect or remove it manually only when it is known
to be a disposable local development file.

## Final acceptance

An agent may report `READY` only after development acceptance passes. A missing
GitHub login, Git, Python, or deliberately optional Docker is an actionable
external prerequisite, not a reason to use production resources.
