"""Stage 2 qualification fixture entry point.

Use the management command, not this file as a stand-alone Python program:

    python manage.py generate_public_catalog_stage2_qualification --confirm-isolated

The command refuses a non-empty catalog and is intended exclusively for an
isolated, migrated PostgreSQL 16 database.
"""

if __name__ == "__main__":
    raise SystemExit(
        "Run `python manage.py generate_public_catalog_stage2_qualification "
        "--confirm-isolated`, not this module directly."
    )
