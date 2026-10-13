"""Validate a restored DenisStock stack from INSIDE its web container.

Run: python manage.py shell < validate_restore.py
Inputs (copied in by offline_restore.sh, never secrets):
  /tmp/dr/manifest.json          signed manifest of the restored backup run
  /tmp/dr/private-inventory.json optional [relative_path, sha256, bytes] rows
Prints one JSON report on the last line. Counts and hashes only: no customer
names, no file names, no field values.
"""

import hashlib
import json
from collections import Counter
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import connection, models
from django.db.migrations.loader import MigrationLoader

from apps.operations.emergency_state import (
    business_state_marker,
    media_tree_sha256,
    migration_state,
)

report = {"checks": {}, "facts": {}}


def check(name, passed, **facts):
    report["checks"][name] = bool(passed)
    if facts:
        report["facts"][name] = facts


manifest = json.loads(Path("/tmp/dr/manifest.json").read_text())

# Source and schema
check("app_commit_matches_backup", settings.DENSTOCK_APP_COMMIT[:12] == manifest["app_commit"][:12],
      running=settings.DENSTOCK_APP_COMMIT[:12], backup=manifest["app_commit"][:12])
applied = migration_state()
check("applied_migrations_equal_backup", applied["applied"] == manifest["migration_state"],
      applied=len(applied["applied"]))
loader = MigrationLoader(connection, ignore_no_migrations=True)
disk = set(loader.disk_migrations)
applied_set = {tuple(row) for row in applied["applied"]}
check("no_unapplied_or_unknown_migrations", disk == applied_set,
      unapplied=len(disk - applied_set), unknown=len(applied_set - disk))

# Business data: the same fingerprint the backup signed
marker = business_state_marker()
check("business_fingerprint_equals_signed_manifest", marker == manifest["data_state"],
      business_sha256=marker["business_sha256"][:16], generation=marker["business_generation"])

# Every table readable, counts, FK orphans
with connection.cursor() as cursor:
    cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
    tables = [row[0] for row in cursor.fetchall()]
    total = 0
    for table in tables:
        cursor.execute(f'SELECT count(*) FROM "{table}"')
        total += cursor.fetchone()[0]
    cursor.execute("""
        SELECT c.conrelid::regclass::text, a.attname, c.confrelid::regclass::text, af.attname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
        JOIN pg_attribute af ON af.attrelid=c.confrelid AND af.attnum=c.confkey[1]
        JOIN pg_namespace n ON n.oid=c.connamespace
        WHERE c.contype='f' AND n.nspname='public' AND array_length(c.conkey,1)=1""")
    fks = cursor.fetchall()
    orphans = 0
    for child, col, parent, pcol in fks:
        cursor.execute(
            f'SELECT count(*) FROM {child} ch WHERE ch."{col}" IS NOT NULL AND NOT EXISTS '
            f'(SELECT 1 FROM {parent} p WHERE p."{pcol}" = ch."{col}")'
        )
        orphans += cursor.fetchone()[0]
    cursor.execute(
        "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
        "WHERE n.nspname='public' AND NOT c.convalidated"
    )
    not_valid = cursor.fetchone()[0]
    cursor.execute("SELECT count(*) FROM pg_indexes WHERE schemaname='public'")
    indexes = cursor.fetchone()[0]
expected_tables = {m._meta.db_table for m in apps.get_models(include_auto_created=True)
                   if m._meta.managed}
check("all_model_tables_exist", expected_tables <= set(tables),
      tables=len(tables), rows=total, indexes=indexes)
check("foreign_keys_without_orphans", orphans == 0 and not_valid == 0,
      foreign_keys=len(fks), orphans=orphans, not_validated=not_valid)

# Stock cache equals the primary records (existing read-only command)
try:
    call_command("check_stock_balance")
    stock_ok = True
except SystemExit as exc:
    stock_ok = exc.code in (0, None)
except Exception:  # noqa: BLE001
    stock_ok = False
check("stock_balance_cache_matches_primary_records", stock_ok)

# Representative business objects (counts only)
counts = {}
for label in ("catalog.PartType", "inventory.StockLot", "inventory.PartItem",
              "inventory.StockMovement", "customers.Customer", "sales.Sale",
              "repairs.RepairOrder", "customer_requests.CustomerRequest",
              "warehouse.StorageLocation", "accounts.User"):
    try:
        counts[label] = apps.get_model(label).objects.count()
    except LookupError:
        counts[label] = None
report["facts"]["business_objects"] = counts

# Public media tree equals the signed manifest
media_root = Path(settings.MEDIA_ROOT)
check("public_media_tree_equals_manifest",
      media_tree_sha256(media_root) == manifest["media_tree_sha256"],
      files=sum(1 for p in media_root.rglob("*") if p.is_file()))

# Private media: every file's SHA-256 equals the independent inventory
private_root = Path(settings.PRIVATE_MEDIA_ROOT)
inventory_path = Path("/tmp/dr/private-inventory.json")
if inventory_path.exists():
    expected = {row[0]: (row[1], row[2]) for row in json.loads(inventory_path.read_text())}
    actual = {
        p.relative_to(private_root).as_posix(): (
            hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size)
        for p in private_root.rglob("*") if p.is_file()
    }
    check("private_media_files_equal_inventory", actual == expected,
          expected=len(expected), restored=len(actual))
elif manifest.get("private_media_status") == "included":
    # private_media.tar.gz inside the signed run: its tree hash is signed.
    tree = media_tree_sha256(private_root)
    check("private_media_files_equal_inventory", tree == manifest["private_media_tree_sha256"],
          expected=manifest.get("private_media_file_count"),
          restored=sum(1 for p in private_root.rglob("*") if p.is_file()))
else:
    check("private_media_files_equal_inventory", False, reason="no inventory supplied")

# Every FileField reference resolves to a restored file
private_location = private_root.resolve()
missing = Counter()
references = 0
for model in apps.get_models():
    if not model._meta.managed:
        continue
    for field in model._meta.concrete_fields:
        if not isinstance(field, models.FileField):
            continue
        location = Path(getattr(field.storage, "location", "") or "/nonexistent").resolve()
        root = private_root if location == private_location else media_root
        names = (model._base_manager.exclude(**{field.attname: ""})
                 .exclude(**{f"{field.attname}__isnull": True})
                 .values_list(field.attname, flat=True))
        for name in names.iterator():
            references += 1
            if not (root / name).is_file() and not (media_root / name).is_file():
                missing[f"{model._meta.label_lower}.{field.name}"] += 1
check("file_references_resolve", not missing, references=references,
      missing_by_field=dict(missing))

report["passed"] = all(report["checks"].values())
print("DR-VALIDATION " + json.dumps(report, ensure_ascii=False, sort_keys=True))
