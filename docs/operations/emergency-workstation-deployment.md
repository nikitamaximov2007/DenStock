# Emergency Local Workstation Deployment

## Supported topology

One warehouse PC is provisioned as `primary`. It owns the local PostgreSQL 16
database, verified standby slots, media, `.emergency/control.json` and the
only configuration that can enter `ACTIVE`. Other PCs are LAN clients and open
the Primary URL. A replacement PC must be provisioned as `secondary`; the
application and launcher both refuse its activation.

This is a single-writer failover design. It is not database replication and it
does not permit two independent emergency writers from one production backup.

## Runtime

The supported Windows runtime is **WSL2 Ubuntu plus Docker Engine inside WSL**.
Docker Desktop is optional and is not required. The repository stays on a
Windows data drive and WSL accesses it through `/mnt/<drive>/...`; PostgreSQL
data remains in the isolated `denstock-emergency` Docker volume.

Minimum Primary capacity:

- Windows 10/11 with WSL2 support;
- 8 GB RAM;
- 30 GB free disk on the drive containing the release and `.emergency`;
- stable private LAN IPv4 address, preferably DHCP reservation or static IP;
- a dedicated Windows account for the responsible warehouse operator.

Do not install this runtime on a developer PC merely because it has the source
checkout. Do not expose PostgreSQL. Do not add router port forwarding.

## Provision Primary

Obtain a clean checkout or release bundle at the exact production commit. The
release operator, not an ordinary warehouse employee, supplies the backup
source and production probe token through an approved secret channel.

Open Administrator PowerShell and run once:

```powershell
F:\DenisStockEmergency\scripts\operations\Install-DenisStock-EmergencyWorkstation.ps1 `
  -RepoRoot F:\DenisStockEmergency `
  -BackupSource 'yandex-s3:denstock-backups-nikita' `
  -ReleaseSource 'origin' `
  -ProductionUrl 'https://185-250-44-206.sslip.io' `
  -PrimaryLanAddress '192.168.100.10' `
  -AppCommit '0fd18913234d344ea3ae44cbc84bb6dc411bc3ad' `
  -ManifestPublicKeyPath 'F:\ApprovedSecrets\production-manifest-ed25519-public.pem' `
  -ManifestSigningKeyId 'production-ed25519-2026-01' `
  -Role primary -ConfirmPrimary -InstallWslRuntime -CreateTasks
```

The first WSL install can require a Windows reboot. Re-run the same command
after the reboot. The installer creates `.env.emergency` with new local
secrets, limits both it and `.emergency` backups to the provisioning
administrator and Administrators, adds a Private-profile `LocalSubnet` firewall
rule for the chosen port, creates two control shortcuts on that administrator's
desktop and registers refresh at logon, 07:00 and 19:00.

The production signing private key is never copied to this PC. The installer
copies only the administrator-supplied pinned public key into protected
`.emergency/trusted/` storage and creates a stable UUID in protected
`.emergency/workstation-id.txt`. On a normal update the installer refuses to
replace either trust anchor or identity. The Django process verifies that the
env UUID matches this protected file, so copying `.env.emergency` from Primary
A onto an already-provisioned B does not impersonate A.

`rclone` is intentionally not configured by this script because its Yandex
credentials must never be placed in source control. Configure the approved
remote only in the responsible Windows profile, then use the status shortcut.

`ReleaseSource` is a controlled Git remote or local bare repository containing
every approved production release. It is not optional: when a backup manifest
has a newer `app_commit`, refresh fetches exactly that commit before restoring a
candidate. If code fetch, checkout, restore or validation fails, the previous
checkout and its READY standby are restored. This prevents a valid backup B
from being presented with application code A.

## Provision Secondary or LAN clients

A cold replacement uses `-Role secondary`; its bind host is forced to
`127.0.0.1` and activation is blocked by both the launcher and Django
lifecycle. Promote it only through a documented incident decision, after the
old Primary has been made unavailable and its final export preserved. The
local role protects a configured secondary, but it cannot prove that two
separately provisioned machines were both declared `primary` while the network
is down. Do not provision another primary until a production-authorized primary
identity is added to the backup manifest and enforced during activation.

Before the first signed backup, a production administrator generates and backs
up an Ed25519 signing key through the approved secret process, provisions the
public key above, then explicitly authorizes the printed workstation UUID:

```bash
docker compose exec web python manage.py authorize_emergency_primary \
  --workstation-id '<WORKSTATION-UUID>' --actor '<ADMIN>' \
  --confirm 'НАЗНАЧИТЬ-EMERGENCY-PRIMARY'
```

Replacement A to B is a production-side promotion: preserve A's final export,
make A unavailable, authorize B (which increments the authorization epoch),
create a fresh signed standby for B, then wipe A's local standby before it can
be returned to service. To revoke all authorization, use
`revoke_emergency_primary --actor '<ADMIN>' --confirm 'ОТОЗВАТЬ-EMERGENCY-PRIMARY'`.
No migration or workstation provisioning step authorizes a Primary.

A fully isolated former Primary can still start with an older genuine signed
manifest because it cannot learn a later promotion. This is a known offline
limitation, not a permission to operate two writers: controlled replacement,
decommissioning and a fresh-standby policy are mandatory. A Windows or WSL
administrator who can replace application code, the protected identity and the
pinned key controls that endpoint and is outside this software-only boundary.

LAN client PCs do not receive PostgreSQL, `.env.emergency`, rclone, standby
data or failback controls. Create a shortcut containing only:

```text
http://PRIMARY-LAN-IP:8080/
```

Use the fixed Primary LAN address, keep the Windows firewall remote scope as
`LocalSubnet`, and verify login from one client before declaring the Primary
ready. DenisStock users and roles are restored from the standby backup, so no
external identity provider is needed while offline.

## Operating checks

`DenisStock - Аварийный статус` reports the active standby timestamp, age,
commit, verification state and lifecycle. Interpret it as follows:

- GREEN: verified active standby and age within
  `DENSTOCK_EMERGENCY_STALE_WARNING_HOURS`.
- YELLOW: verified standby exists but is older than that configurable value.
- RED: no verified standby, a malformed control file, incompatible commit or
  lifecycle failure. Do not activate.

The scheduled task calls `Emergency-Standby-Refresh.ps1`. It is noninteractive
and fails closed if an offline lifecycle exists. Refresh restores into a new
candidate database and atomically swaps control only after manifest, hashes,
migrations, database and media checks pass. The prior READY standby remains
available after any failed download, restore or validation.

## Offline capability matrix

| Function | Emergency ACTIVE | Notes |
|---|---|---|
| Parts, stock, locations, barcode and scanner | PASS | Uses local PostgreSQL and local media. |
| Receiving, transfers, write-offs and inventory | PASS | Existing write guard permits only ACTIVE. |
| Multi-item sales, cancellation and reservations | PASS | One local backend serves every LAN client. |
| Repairs, multi-item repair and returns | PASS | Same local business services and history. |
| Cell/section recount | PASS | Local PostgreSQL locking remains in effect. |
| Customers, Client 360, reports, pricing and BRP catalog | PASS | Data is from the verified standby snapshot. |
| Catalog Import | PASS | File and import history remain local until freeze. |
| Attachments, private media and printing | PASS/LIMITED | Local files work; physical printer availability is workstation-specific. |
| Roles, permissions and login | PASS | Users and password hashes are in the standby database. |
| AI Support and external integrations | UNAVAILABLE OFFLINE BY DESIGN | Disabled before any network call. |
| Offsite upload and production failback | ADMIN ONLY | No automatic restore or production overwrite. |

## Drill and recovery

Use a synthetic backup and isolated test Primary for drills. Never use drill
transactions for production failback. Verify login, stock workflow, multi-item
sale, multi-item repair, a customer and a media attachment while `ACTIVE`; then
freeze, verify final export and deliberately change the production marker in
the test fixture to prove `CONFLICT`. Discard the drill slot and refresh a new
READY standby.

After a normal Primary reboot, Docker Engine starts through WSL systemd and the
Compose services restart. `STANDBY` never auto-promotes. `ACTIVE` and `FROZEN`
survive because lifecycle state is stored in local PostgreSQL and control.json.

## Failback boundary

Only a frozen, verified package can be checked. `ELIGIBLE` never restores
production automatically. A production administrator must take a fresh
production backup, enter maintenance, independently review the package and use
the controlled production runbook. `CONFLICT` and `BLOCKED` always require
manual reconciliation.
