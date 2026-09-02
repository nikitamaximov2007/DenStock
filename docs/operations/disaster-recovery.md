# DenisStock Disaster Recovery

## Scenario

This runbook covers the loss of both the administrator laptop and the
production VPS. Recovery starts with a new computer and a new VPS. Do not use
any surviving production checkout, local worktree, or local backup as an
assumed source of truth.

The independent sources are:

1. GitHub repository `nikitamaximov2007/DenStock`.
2. A signed offsite backup in Yandex Object Storage.
3. An independently stored administrator recovery kit.
4. Recovery access to the GitHub, Yandex, VPS-provider, and password-manager
   accounts.

If any required asset below is unavailable, stop before a restore. Do not
replace an active server or overwrite a database to "try" recovery.

## Required independent assets

Store these outside the laptop, VPS, Git repository, and normal backup bucket:

| Asset | Why it is needed | Recovery rule |
| --- | --- | --- |
| GitHub account recovery | Clone the application source and obtain the exact commit | Keep recovery codes and verified second factor in a password manager. |
| Yandex Object Storage account recovery | Download an offsite backup | Keep a separate recovery path for the account and S3/rclone credentials. |
| Encrypted signing-key recovery material | Verify and continue signing production manifests | Keep an encrypted copy and its passphrase in different independent locations. |
| Trusted signing public key and fingerprint | Verify a backup without trusting the backup itself | Keep with the recovery kit. |
| VPS-provider recovery | Create a replacement server and recover IP access | Keep account recovery and billing contacts outside the VPS. |
| Domain/DNS recovery, if a managed domain is introduced | Move traffic to the replacement server | Keep registrar and DNS account recovery separately. |

The DR reader identity for Object Storage must have only `ListBucket` and
`GetObject` equivalents for the backup bucket and prefix. It must not have
create, overwrite, delete, lifecycle, retention, bucket-policy, or IAM rights.

The current recovery kit must contain `production-signing-key.enc`, the public
key fingerprint, and instructions only. It must never contain plaintext private
keys, `.env`, database passwords, rclone configuration, or production dumps.

## GitHub recovery

1. Recover the GitHub account through its external recovery mechanism.
2. Clone the canonical remote into a new empty directory:

   ```bash
   git clone https://github.com/nikitamaximov2007/DenStock.git denstock
   cd denstock
   git fetch --tags origin
   ```

3. Do not use `main` blindly. The signed backup manifest is the authority for
   the application commit that produced its data.
4. Confirm that `app_commit` and `git_commit` in the manifest are identical,
   reachable from GitHub, and resolve to a full commit hash.
5. Check out that exact hash detached or through a temporary recovery branch.

Public visibility is not a recovery control. Changing repository visibility,
branch protection, or deleting branches requires a separate approved GitHub
maintenance task.

## Account recovery checklist

Before declaring the kit independent, confirm without recording secret values:

- GitHub: recovery codes, verified second factor, and repository access.
- Yandex Object Storage: a separate read-only DR identity can list and download
  the backup bucket, and account recovery is stored outside the VPS.
- VPS provider: owner account, recovery contact, billing recovery, and access
  to create a new VPS.
- Password manager: emergency access and the signing-key passphrase location.
- AI Support: either its separate account and launcher configuration are
  recoverable, or the documented plan is to restore the core service with AI
  disabled.

## Obtain and verify the latest signed backup

1. Configure read-only rclone access to
   `yandex-s3:denstock-backups-nikita` on the new machine or new VPS.
2. List runs and download a candidate into an empty, non-production directory.
3. Require `manifest.json`, `db.dump`, and `media.tar.gz` before proceeding.
4. Verify SHA-256 values from the manifest for the database dump and media
   archive.
5. Verify the manifest signature with the independently stored public key.
   The expected production identity is `ed25519`, key ID `production-1`, and
   the trusted public DER SHA-256 fingerprint is
   `5615837ef355d2d1881508434980efac31f1c467acb3d31c57101ced3ee5d5b1`.
6. Run the application verifier against the downloaded run:

   ```bash
   docker compose exec web python manage.py verify_backup <run_id>
   ```

7. Stop if any file, hash, signature, source environment, application commit,
   or PostgreSQL compatibility check differs from the manifest contract.

Never use the production private signing key merely to verify a backup.

## Provision a new VPS

1. Create a new Ubuntu 24.04 VPS with Docker Engine, Docker Compose, Git, and
   firewall rules for SSH, HTTP, and HTTPS only.
2. Clone the exact commit identified by the verified backup.
3. Create a new `.env` from `.env.example`. Generate a new PostgreSQL password
   and a new `DJANGO_SECRET_KEY`. A new secret key invalidates old browser
   sessions, which is expected after a disaster.
4. Configure `DATABASE_URL` for a fresh PostgreSQL 16 container. The restore
   target must be a new database, never an existing or production database.
5. Keep AI Support disabled unless its separate launcher, external account, and
   configuration have been intentionally recovered. The core warehouse service
   must not depend on an unverified AI configuration.
6. Start the stack only on the replacement host:

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

## Restore DB and media

Use only the verified copy downloaded to the new VPS:

```bash
docker compose exec web python manage.py restore_db backups/<run_id>/db.dump --yes
docker compose exec web python manage.py restore_media backups/<run_id>/media.tar.gz --yes
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py check
docker compose exec web python manage.py ops_check
```

PostgreSQL 16 is the normal target. The repository contains a controlled
pg_restore 17 fallback only for an older custom archive that requires it.
Treat every restore error other than the documented compatibility case as fatal.

## Signing-key recovery and rotation

Recover `production-signing-key.enc` only on the replacement production host,
using the passphrase from the independent password manager. Keep plaintext key
material out of Git, backups, logs, chat, and workstations. Verify its public
fingerprint before use. After recovery, create a fresh signed backup. Plan a
separate, approved key rotation if the old host compromise is suspected.

## Verify production

Before routing users to the new VPS, confirm:

1. `/healthz/`, `manage.py check`, and `ops_check` pass.
2. Database identity, migration fingerprint, DB hash, media hash, and business
   marker match the verified manifest where applicable.
3. Read-only smoke succeeds for login, search, stock, customers, reports,
   receipts, repairs, and Customs.
4. A new signed backup is created and uploaded offsite from the replacement.
5. The new backup records the recovered application commit.

For the current `sslip.io` URL, the new public IP changes the URL itself. If a
managed DNS name is adopted, update its DNS only after all verification passes.

## Final acceptance

Record the exact restored commit, backup run ID, signature verification result,
database and media hashes, replacement VPS identity, and the first successful
post-recovery offsite backup. Then restore normal backup scheduling and access
controls.

## Things never to do

- Never overwrite an active production database during a drill.
- Never restore an unsigned or hash-mismatched backup.
- Never use a backup manifest as its own trust anchor.
- Never commit `.env`, rclone credentials, database passwords, recovery
  passphrases, or private signing keys.
- Never copy the production private signing key from the old VPS as a shortcut.
- Never delete backup runs, GitHub branches, tags, or repository history during
  disaster recovery.
