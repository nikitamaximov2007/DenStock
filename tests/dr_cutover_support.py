"""Real-process harness for the offline DR cutover tests (synthetic data only).

Each DR step runs as its own ``python manage.py`` process against a throwaway
PostgreSQL 16 cluster, so a fault point can really SIGKILL/SIGTERM it and the
next step starts from whatever the dead process left on disk and in the
cluster.  The cluster comes from ``DENSTOCK_TEST_DATABASE_URL``; databases are
created and dropped here by name with a per-run random prefix.

``seed`` and ``probe_app`` run INSIDE those subprocesses (via ``manage.py shell``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg import sql

REPO = Path(__file__).resolve().parent.parent
PG_BIN = "/opt/homebrew/opt/postgresql@16/bin"
PART_NAME = "DR-SYNTHETIC-PART"
CUSTOMER_NAME = "DR Synthetic Customer"
PUBLIC_PHOTO = b"synthetic public photo"
PRIVATE_SCAN = b"synthetic private scan"


def cluster_from_env() -> dict | None:
    url = os.environ.get("DENSTOCK_TEST_DATABASE_URL", "").strip()
    if not url.startswith("postgres"):
        return None
    parts = urlsplit(url)
    return {
        "host": parts.hostname or "127.0.0.1",
        "port": parts.port or 5432,
        "user": parts.username or "postgres",
        "password": parts.password or "",
    }


def admin(cluster: dict, dbname: str = "postgres"):
    return psycopg.connect(dbname=dbname, autocommit=True, **cluster)


def database_url(cluster: dict, name: str) -> str:
    auth = cluster["user"] + (f":{cluster['password']}" if cluster["password"] else "")
    return f"postgres://{auth}@{cluster['host']}:{cluster['port']}/{name}"


def databases(cluster: dict, prefix: str) -> dict[str, int]:
    with admin(cluster) as conn:
        rows = conn.execute(
            "SELECT datname, oid FROM pg_database WHERE datname LIKE %s", (prefix + "%",)
        ).fetchall()
    return dict(rows)


def comment(cluster: dict, name: str):
    with admin(cluster) as conn:
        row = conn.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
            (name,),
        ).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def drop_databases(cluster: dict, prefix: str) -> None:
    with admin(cluster) as conn:
        for name in databases(cluster, prefix):
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            ))


def write_keys(directory: Path) -> dict:
    private = Ed25519PrivateKey.generate()
    private_path, public_path = directory / "signing.pem", directory / "public.pem"
    private_path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return {
        "DENSTOCK_MANIFEST_SIGNING_KEY_PATH": str(private_path),
        "DENSTOCK_MANIFEST_PUBLIC_KEY_PATH": str(public_path),
        "DENSTOCK_MANIFEST_SIGNING_KEY_ID": "dr-cutover-test-1",
    }


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def killed(self) -> bool:
        return self.returncode < 0

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


@dataclass
class Deployment:
    """One simulated deployment: a live database and two live roots."""

    cluster: dict
    root: Path
    prefix: str
    keys: dict
    live_db: str
    env_extra: dict = field(default_factory=dict)

    @property
    def media(self) -> Path:
        return self.root / "vol-media" / "_data"

    @property
    def private(self) -> Path:
        return self.root / "vol-private" / "_data"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    def env(self, mode: str = "development", **extra) -> dict:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("DENSTOCK_", "DJANGO_", "DATABASE_URL", "PRIVATE_MEDIA"))
        }
        env.update(self.keys)
        env.update({
            "PATH": f"{PG_BIN}:{env.get('PATH', '')}",
            "DATABASE_URL": database_url(self.cluster, self.live_db),
            "DENSTOCK_MEDIA_ROOT": str(self.media),
            "PRIVATE_MEDIA_ROOT": str(self.private),
            "BACKUP_ROOT": str(self.backups),
            "DENSTOCK_APP_COMMIT": "d" * 40,
            "DENSTOCK_INSTANCE_ID": "dr-cutover-synthetic",
            "DENSTOCK_PRODUCTION_DB_HOSTS": self.cluster["host"],
            "DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS": "none.invalid",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if mode == "production":
            env.update({
                "DJANGO_SETTINGS_MODULE": "config.settings.prod",
                "DENSTOCK_MODE": "production",
                "DJANGO_SECRET_KEY": "synthetic-dr-cutover-secret-key-0123456789",
                "DJANGO_ALLOWED_HOSTS": "testserver",
            })
        elif mode == "public":
            env.update({
                "DJANGO_SETTINGS_MODULE": "config.settings.public",
                "DENSTOCK_MODE": "public-catalog",
                "DJANGO_SECRET_KEY": "synthetic-dr-cutover-secret-key-0123456789",
                "DJANGO_PUBLIC_ALLOWED_HOSTS": "testserver",
                "PUBLIC_DATABASE_URL": database_url(self.cluster, self.live_db),
                "DENSTOCK_PUBLIC_DATABASE_HOSTS": self.cluster["host"],
                "DJANGO_SECURE_COOKIES": "false",
            })
        else:
            env.update({
                "DJANGO_SETTINGS_MODULE": "config.settings.dev",
                "DENSTOCK_MODE": "development",
            })
        env.update(self.env_extra)
        env.update(extra)
        return env

    def manage(self, *args, mode="development", fault=None, action="SIGKILL",
               timeout=240, **extra) -> Result:
        env = self.env(mode, **extra)
        if fault:
            env["DENSTOCK_DR_FAULT"] = fault
            env["DENSTOCK_DR_FAULT_ACTION"] = action
        completed = subprocess.run(
            [sys.executable, "manage.py", *args], cwd=REPO, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        return Result(completed.returncode, completed.stdout, completed.stderr)

    def spawn(self, *args, fault=None, action="SIGKILL", **extra) -> subprocess.Popen:
        env = self.env(**extra)
        if fault:
            env["DENSTOCK_DR_FAULT"] = fault
            env["DENSTOCK_DR_FAULT_ACTION"] = action
        return subprocess.Popen(
            [sys.executable, "manage.py", *args], cwd=REPO, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def shell(self, code: str, *, mode="development", **extra) -> Result:
        return self.manage("shell", "-c", code, mode=mode, **extra)

    # --- staged slot names --------------------------------------------------

    def stage_paths(self, tag: str = "a") -> dict:
        return {
            "db": f"{self.live_db}_st_{tag}",
            "media": self.root / "vol-media" / f"stage-{tag}",
            "private": self.root / "vol-private" / f"stage-{tag}",
            "journal": self.root / "dr" / f"stage-{tag}.journal",
        }

    def stage(self, run: Path, tag: str = "a", **kwargs) -> Result:
        paths = self.stage_paths(tag)
        (self.root / "dr").mkdir(exist_ok=True)
        return self.manage(
            "dr_stage_restore", str(run), "--target-db", paths["db"],
            "--media-dir", str(paths["media"]), "--private-media-dir", str(paths["private"]),
            "--journal", str(paths["journal"]), **kwargs,
        )

    def set_write_state(self, name: str, state: str) -> None:
        with admin(self.cluster, name) as conn:
            conn.execute(
                "UPDATE operations_deploymentstate SET write_state = %s WHERE id = 1", (state,)
            )

    def first_name_marker(self, name: str) -> str:
        with admin(self.cluster, name) as conn:
            return conn.execute(
                "SELECT first_name FROM accounts_user WHERE username = 'dr-admin'"
            ).fetchone()[0]


def build_source(cluster: dict, root: Path, prefix: str) -> tuple[Deployment, Path]:
    """Migrate + seed a production-mode source deployment and back it up signed."""
    root.mkdir(parents=True, exist_ok=True)
    keys = write_keys(root)
    source = Deployment(cluster, root / "source", prefix, keys, f"{prefix}source")
    source.media.mkdir(parents=True)
    source.private.mkdir(parents=True)
    with admin(cluster) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(source.live_db)))
    for args in (("migrate", "--noinput"),):
        result = source.manage(*args, mode="production", timeout=600)
        assert result.returncode == 0, result.output
    result = source.shell("from tests.dr_cutover_support import seed; seed()", mode="production")
    assert result.returncode == 0, result.output
    result = source.manage("backup_all", mode="production")
    assert result.returncode == 0, result.output
    runs = sorted(path for path in source.backups.iterdir() if (path / "manifest.json").exists())
    assert len(runs) == 1, runs
    return source, runs[0]


def new_deployment(source: Deployment, root: Path, tag: str) -> Deployment:
    """An OLD generation that differs from the backup: copied DB, marked rows/files."""
    cluster = source.cluster
    live = Deployment(cluster, root, source.prefix, source.keys, f"{source.prefix}live_{tag}")
    with admin(cluster) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(live.live_db), sql.Identifier(source.live_db)
        ))
    with admin(cluster, live.live_db) as conn:
        conn.execute("UPDATE accounts_user SET first_name = 'OLD-GENERATION'")
    shutil.copytree(source.media, live.media)
    shutil.copytree(source.private, live.private)
    (live.media / "old-generation-only.txt").write_text("old")
    (live.private / "old-generation-only.txt").write_text("old")
    live.set_write_state(live.live_db, "maintenance")
    return live


# --- Code executed inside subprocesses -------------------------------------------


def seed() -> None:
    from decimal import Decimal

    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.catalog.models import (
        Category,
        Manufacturer,
        PartNumber,
        PartType,
        PartTypeImage,
        Unit,
    )
    from apps.customer_requests.models import (
        CustomerRequest,
        TelegramConversation,
        TelegramMessage,
    )
    from apps.operations.models import DeploymentState

    DeploymentState.get_solo()
    get_user_model().objects.create_superuser(
        "dr-admin", "dr-admin@example.invalid", uuid.uuid4().hex, first_name="BACKUP-GENERATION"
    )
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    part = PartType.objects.create(
        name=PART_NAME,
        category=category,
        unit=Unit.objects.get(name="Штука"),
        manufacturer=Manufacturer.objects.get_or_create(name="BRP")[0],
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("10000.00"),
    )
    PartNumber.objects.create(part=part, value="DR-448", is_primary=True)
    photo = Path(settings.MEDIA_ROOT) / "part-types" / "dr-photo.jpg"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(PUBLIC_PHOTO)
    PartTypeImage.objects.create(part=part, image="part-types/dr-photo.jpg")
    request = CustomerRequest.objects.create(
        customer_name=CUSTOMER_NAME, customer_phone="0", preferred_messenger="telegram",
        privacy_policy_version="v", personal_data_consent_version="v",
        consent_purpose="synthetic", consent_accepted_at=timezone.now(),
        submission_key_hash="d" * 64,
    )
    conversation = TelegramConversation.objects.create(request=request)
    scan = Path(settings.PRIVATE_MEDIA_ROOT) / "customer_requests" / "dr-scan.pdf"
    scan.parent.mkdir(parents=True, exist_ok=True)
    scan.write_bytes(PRIVATE_SCAN)
    TelegramMessage.objects.create(
        conversation=conversation, direction=TelegramMessage.Direction.OPERATOR,
        attachment="customer_requests/dr-scan.pdf",
    )


def probe_app() -> None:
    """Start like the WSGI server, then read restored data through real views."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.signals import user_logged_in
    from django.test import Client
    from django.urls import reverse

    import config.wsgi  # noqa: F401  (runs the startup guard)
    from apps.catalog.models import PartType
    from apps.customer_requests.models import CustomerRequest, TelegramMessage

    # Writes are still blocked (maintenance); logging in must not need one.
    user_logged_in.disconnect(dispatch_uid="update_last_login")
    client = Client()
    client.force_login(get_user_model().objects.get(username="dr-admin"))
    part = PartType.objects.get(name=PART_NAME)
    request = CustomerRequest.objects.get(customer_name=CUSTOMER_NAME)
    part_page = client.get(reverse("part_detail", args=[part.pk]))
    request_page = client.get(reverse("customer_request_detail", args=[request.pk]))
    message = TelegramMessage.objects.get(conversation__request=request)
    with message.attachment.open("rb") as handle:
        attachment = handle.read()
    image = part.images.get()
    with image.image.open("rb") as handle:
        photo = handle.read()
    print(json.dumps({
        "part_status": part_page.status_code,
        "part_has_name": PART_NAME in part_page.content.decode(),
        "request_status": request_page.status_code,
        "request_has_customer": CUSTOMER_NAME in request_page.content.decode(),
        "attachment_ok": attachment == PRIVATE_SCAN,
        "photo_ok": photo == PUBLIC_PHOTO,
        "first_name": get_user_model().objects.get(username="dr-admin").first_name,
    }))
