#!/usr/bin/env python3
"""Write NEW random env files for a restore when the old secrets are unavailable.

    fresh_env.py <out dir (new)> <app commit> [--db-user denstock] [--db-name denstock]
                 [--public-role denstock_public]

Used by the synthetic drill and by a recovery from GitHub alone: the database
dump restores with ``--no-owner`` and the public role is re-created, so new
passwords and a new DJANGO_SECRET_KEY are enough to start the service (DenisStock
keeps no data encrypted with SECRET_KEY; sessions are simply invalidated).
Bot tokens, the domain and the signing key are NOT invented here: the operator
adds them before the service goes public. Files are 0600, values never printed.
"""

import argparse
import os
import secrets

parser = argparse.ArgumentParser()
parser.add_argument("out")
parser.add_argument("commit")
parser.add_argument("--db-user", default="denstock")
parser.add_argument("--db-name", default="denstock")
parser.add_argument("--public-role", default="denstock_public")
args = parser.parse_args()
os.makedirs(args.out, mode=0o700)
db_pw, pub_pw = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
user, name = args.db_user, args.db_name


def write(filename, rows):
    path = os.path.join(args.out, filename)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fh:
        fh.write("\n".join(f"{k}={v}" for k, v in rows) + "\n")


write("db.env", [("POSTGRES_DB", name), ("POSTGRES_USER", user), ("POSTGRES_PASSWORD", db_pw)])
write("web.env", [
    ("DJANGO_SETTINGS_MODULE", "config.settings.prod"), ("DJANGO_DEBUG", "false"),
    ("DJANGO_SECRET_KEY", secrets.token_urlsafe(40)), ("DJANGO_ALLOWED_HOSTS", "localhost"),
    ("POSTGRES_DB", name), ("POSTGRES_USER", user), ("POSTGRES_PASSWORD", db_pw),
    ("DATABASE_URL", f"postgres://{user}:{db_pw}@db:5432/{name}"),
    ("DENSTOCK_MODE", "production"), ("DENSTOCK_APP_COMMIT", args.commit),
    ("PRIVATE_MEDIA_ROOT", "/app/private_media"), ("TIME_ZONE", "Europe/Moscow"),
])
write("public.env", [
    ("DJANGO_SECRET_KEY", secrets.token_urlsafe(40)),
    ("PUBLIC_DATABASE_URL", f"postgres://{args.public_role}:{pub_pw}@db:5432/{name}"),
    ("DJANGO_SECURE_COOKIES", "false"),
])
