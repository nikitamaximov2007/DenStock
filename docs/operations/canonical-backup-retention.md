# Canonical retention of production backups

`scripts/operations/denstock-backup-capped` is the Git source for
`/usr/local/sbin/denstock-backup-capped`. It is a host task used by the existing
`denstock-backup.service`; it does not replace the application's backup creator.

Retention keeps every run from the last 24 hours, one run for each of the six
preceding days, then one run per ISO week. The newest run is never purged.

S3 versioning means a purge may make an object invisible while old versions still
consume billable bytes. Therefore the script measures `rclone size --s3-versions`
and runs version-aware cleanup. Yandex may recalculate bucket size asynchronously;
repeat the diagnostic after the provider has settled:

```bash
rclone size "$DENSTOCK_BACKUP_REMOTE" --s3-versions --json
journalctl -u denstock-backup.service -n 100 --no-pager
```

The soft limit is 838860800 bytes. The hard limit stops the job before a later
upload can increase usage. Use `--dry-run` to print purge/cleanup actions without
deleting anything.

To update a host, copy the reviewed repository checkout and run as root:

```bash
bash scripts/operations/install-denstock-backup-capped.sh
```

The installer validates syntax, preserves a timestamped installed copy, writes a
staged root-owned 0755 file, and atomically replaces the target. To roll back,
copy the recorded `.bak.<timestamp>` over the target. Do not run this procedure
from application deployment automation.
