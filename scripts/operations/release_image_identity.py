#!/usr/bin/env python3
"""Build and verify DenisStock images against an exact Git source archive.

This tool prints only public release metadata (Git SHA, source archive digest,
service name and image ID). It never prints Docker environment values or build
output, which may contain deployment details.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ARCHIVE_SHA_LABEL = "com.denstock.source-archive-sha256"
REVISION_LABEL = "org.opencontainers.image.revision"
SERVICES = {"web", "catalog-web", "telegram-bot", "max-bot"}
IMAGE_REFS = {
    "web": "denstock-web",
    "catalog-web": "denstock-catalog-web",
    "telegram-bot": "denstock-telegram-bot",
    "max-bot": "denstock-max-bot",
}
# Each application image has its own Compose selector. Releasing one service
# changes only its own variable, so the other services keep the images they run.
SELECTOR_VARS = {
    "web": "DENSTOCK_WEB_REVISION",
    "catalog-web": "DENSTOCK_CATALOG_WEB_REVISION",
    "telegram-bot": "DENSTOCK_TELEGRAM_BOT_REVISION",
    "max-bot": "DENSTOCK_MAX_BOT_REVISION",
}
SHARED_SELECTOR = "DENSTOCK_BUILD_REVISION"
ALL_PROFILES = ("public-catalog", "telegram-bot", "max-bot")
CONTROL_FILES = ("docker-compose.yml", "docker/Dockerfile",
                 "scripts/operations/release_image_identity.py")


class IdentityError(RuntimeError):
    """A fail-closed source, image or runtime identity check failed."""


def run(argv: list[str], *, cwd: Path = ROOT, env=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, env=env, check=False, capture_output=True)
    except OSError as exc:
        raise IdentityError(f"required tool unavailable: {Path(argv[0]).name}") from exc


def checked(argv: list[str], *, cwd: Path = ROOT, env=None) -> bytes:
    result = run(argv, cwd=cwd, env=env)
    if result.returncode:
        raise IdentityError(f"command failed: {Path(argv[0]).name}")
    return result.stdout


def resolve_commit(repo: Path, requested: str) -> str:
    if not SHA_RE.fullmatch(requested):
        raise IdentityError("release SHA must be a full lowercase 40-character Git SHA")
    resolved = checked(["git", "rev-parse", "--verify", f"{requested}^{{commit}}"], cwd=repo)
    commit = resolved.decode("ascii", errors="strict").strip()
    if commit != requested:
        raise IdentityError("Git resolved a different commit than the requested SHA")
    head = checked(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    if head != requested:
        raise IdentityError("checkout does not equal the requested release SHA")
    for relative in CONTROL_FILES:
        committed = checked(["git", "show", f"{requested}:{relative}"], cwd=repo)
        if (repo / relative).read_bytes() != committed:
            raise IdentityError(f"release control file differs from {requested}: {relative}")
    return commit


def source_archive(repo: Path, commit: str, destination: Path) -> str:
    archive = destination / "source.tar"
    result = run(["git", "archive", "--format=tar", f"--output={archive}", commit], cwd=repo)
    if result.returncode or not archive.is_file():
        raise IdentityError("could not create the exact Git source archive")
    checksum = hashlib.sha256()
    with archive.open("rb") as archive_file:
        for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
            checksum.update(chunk)
    digest = checksum.hexdigest()
    context = destination / "context"
    context.mkdir()
    try:
        with tarfile.open(archive, "r:") as source:
            if "filter" in inspect.signature(tarfile.TarFile.extractall).parameters:
                source.extractall(context, filter="data")
            else:
                source.extractall(context)
    except (tarfile.TarError, OSError) as exc:
        raise IdentityError("could not unpack the exact Git source archive") from exc
    archive.unlink()
    return digest


def compose_images(repo: Path, services: list[str], commit: str) -> dict[str, str]:
    images = {}
    for service in services:
        image = inspect_image(f"{IMAGE_REFS[service]}:{commit}")
        image_id = image.get("Id")
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise IdentityError(f"Docker returned no immutable image ID for {service}")
        images[service] = image_id
    return images


def inspect_image(image_id: str) -> dict:
    raw = checked(["docker", "image", "inspect", image_id]).decode("utf-8")
    try:
        value = json.loads(raw)[0]
    except (ValueError, IndexError, TypeError) as exc:
        raise IdentityError("Docker returned unreadable image metadata") from exc
    return value


def inspect_container(container_id: str) -> dict:
    raw = checked(["docker", "container", "inspect", container_id]).decode("utf-8")
    try:
        value = json.loads(raw)[0]
    except (ValueError, IndexError, TypeError) as exc:
        raise IdentityError("Docker returned unreadable container metadata") from exc
    return value


def _env_map(entries) -> dict[str, str]:
    values = {}
    for entry in entries or []:
        key, sep, value = entry.partition("=")
        if sep:
            values[key] = value
    return values


def verify_image(image: dict, *, commit: str, archive_sha256: str) -> None:
    labels = image.get("Config", {}).get("Labels") or {}
    if labels.get(REVISION_LABEL) != commit:
        raise IdentityError("built image OCI revision does not match requested Git SHA")
    if labels.get(ARCHIVE_SHA_LABEL) != archive_sha256:
        raise IdentityError("built image source archive digest does not match exact Git archive")
    if _env_map(image.get("Config", {}).get("Env")).get("DENSTOCK_APP_COMMIT") != commit:
        raise IdentityError("built image DENSTOCK_APP_COMMIT does not match its OCI revision")


def verify_runtime(repo: Path, service: str, image_id: str, commit: str) -> None:
    verify_release_env(repo, commit, [service])

    raw = checked(["docker", "compose", "ps", "-q", service], cwd=repo)
    containers = [line.strip() for line in raw.decode().splitlines() if line.strip()]
    if len(containers) != 1:
        raise IdentityError(f"expected exactly one running container for {service}")
    container = inspect_container(containers[0])
    if container.get("Image") != image_id:
        raise IdentityError(f"running {service} image ID differs from the verified image")
    if _env_map(container.get("Config", {}).get("Env")).get("DENSTOCK_APP_COMMIT") != commit:
        raise IdentityError(f"running {service} environment differs from the verified image SHA")


def read_env_file(repo: Path) -> dict[str, str]:
    env_file = repo / ".env"
    if not env_file.is_file():
        raise IdentityError("runtime .env file is missing")
    configured = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key and not key.startswith("#"):
            configured[key] = value.strip().strip("'\"")
    return configured


def verify_release_env(repo: Path, commit: str, services: list[str]) -> None:
    """Reject Compose env that would let one release move another service.

    Every application service must have its own explicit selector, so that a
    later release of one service cannot silently re-point the others through
    the shared fallback. The requested services must select the verified SHA.
    """
    configured = read_env_file(repo)
    if "DENSTOCK_APP_COMMIT" in configured:
        raise IdentityError("legacy DENSTOCK_APP_COMMIT override remains in .env")
    if SHARED_SELECTOR in configured:
        raise IdentityError(
            f"shared {SHARED_SELECTOR} remains in .env; set one selector per service instead"
        )
    for service, variable in SELECTOR_VARS.items():
        value = configured.get(variable, "")
        if not SHA_RE.fullmatch(value):
            raise IdentityError(f"{service} has no own full-SHA selector {variable} in .env")
    for service in services:
        if configured[SELECTOR_VARS[service]] != commit:
            raise IdentityError(
                f".env selector {SELECTOR_VARS[service]} does not match the verified release SHA"
            )


def _services(values: str) -> list[str]:
    services = [item.strip() for item in values.split(",") if item.strip()]
    if not services or any(item not in SERVICES for item in services):
        raise IdentityError("services must be a comma-separated list of known app services")
    if len(set(services)) != len(services):
        raise IdentityError("duplicate service names are not allowed")
    return services


def build(repo: Path, requested: str, services: list[str]) -> None:
    commit = resolve_commit(repo, requested)
    with tempfile.TemporaryDirectory(prefix="denstock-release-source-") as temporary:
        staging = Path(temporary)
        archive_sha256 = source_archive(repo, commit, staging)
        env = os.environ.copy()
        env.update({
            "DENSTOCK_BUILD_CONTEXT": str(staging / "context"),
            "DENSTOCK_BUILD_CONTEXT_SHA256": archive_sha256,
        })
        # Only the requested services are pointed at the new commit. The shared
        # fallback is cleared so a stale value cannot leak into another service.
        env.pop(SHARED_SELECTOR, None)
        for service in services:
            env[SELECTOR_VARS[service]] = commit
        checked(["docker", "compose", "build", *services], cwd=repo, env=env)
        image_ids = compose_images(repo, services, commit)
        for service in services:
            image = inspect_image(image_ids[service])
            verify_image(image, commit=commit, archive_sha256=archive_sha256)
            print(f"BUILD IDENTITY PASS service={service} sha={commit} image={image_ids[service]} "
                  f"source_archive_sha256={archive_sha256}")


def verify(repo: Path, requested: str, services: list[str], *, runtime: bool) -> None:
    commit = resolve_commit(repo, requested)
    # Run this before inspecting the image or allowing the operator to proceed
    # to `compose up`; env_file values override Dockerfile ENV at container start.
    verify_release_env(repo, commit, services)
    with tempfile.TemporaryDirectory(prefix="denstock-release-source-check-") as temporary:
        archive_sha256 = source_archive(repo, commit, Path(temporary))
    image_ids = compose_images(repo, services, commit)
    for service in services:
        image = inspect_image(image_ids[service])
        verify_image(image, commit=commit, archive_sha256=archive_sha256)
        if runtime:
            verify_runtime(repo, service, image_ids[service], commit)
        runtime_state = "checked" if runtime else "not-checked"
        print(f"IMAGE IDENTITY PASS service={service} sha={commit} "
              f"image={image_ids[service]} source_archive_sha256={archive_sha256} "
              f"runtime={runtime_state}")


def compose_service_images(repo: Path) -> dict[str, str]:
    """Image references Compose resolves for every application service.

    The rendered configuration contains environment values; only the image
    names are read from it and nothing else is kept or printed.
    """
    argv = ["docker", "compose"]
    for profile in ALL_PROFILES:
        argv += ["--profile", profile]
    raw = checked([*argv, "config", "--format", "json"], cwd=repo)
    try:
        services = json.loads(raw.decode("utf-8"))["services"]
    except (ValueError, KeyError, TypeError) as exc:
        raise IdentityError("Docker Compose returned an unreadable configuration") from exc
    return {name: str(services.get(name, {}).get("image", "")) for name in SELECTOR_VARS}


def running_image_id(repo: Path, service: str) -> str:
    argv = ["docker", "compose"]
    for profile in ALL_PROFILES:
        argv += ["--profile", profile]
    raw = checked([*argv, "ps", "-q", service], cwd=repo)
    containers = [line.strip() for line in raw.decode().splitlines() if line.strip()]
    if len(containers) != 1:
        return ""
    return str(inspect_container(containers[0]).get("Image", ""))


def selector_report(repo: Path) -> list[dict]:
    """Read-only state of every service selector; never prints env values."""
    configured = read_env_file(repo)
    resolved = compose_service_images(repo)
    rows = []
    for service, variable in SELECTOR_VARS.items():
        selector = configured.get(variable, "")
        image_ref = resolved[service]
        row = {"service": service, "variable": variable, "selector": selector or "MISSING",
               "image": image_ref, "present": False, "attested": False, "running": "-",
               "problems": []}
        if not SHA_RE.fullmatch(selector):
            row["problems"].append(f"no own full-SHA selector {variable}")
        elif image_ref != f"{IMAGE_REFS[service]}:{selector}":
            row["problems"].append("Compose image differs from the selector (overlay override?)")
        result = run(["docker", "image", "inspect", image_ref], cwd=repo)
        if result.returncode == 0:
            row["present"] = True
            image = json.loads(result.stdout.decode("utf-8"))[0]
            labels = image.get("Config", {}).get("Labels") or {}
            row["attested"] = labels.get(REVISION_LABEL) == selector
            running = running_image_id(repo, service)
            if running:
                row["running"] = "same" if running == image.get("Id") else "DIFFERENT"
        else:
            row["problems"].append("selected image does not exist")
        rows.append(row)
    if SHARED_SELECTOR in configured:
        rows.append({"service": "-", "variable": SHARED_SELECTOR, "selector": "present",
                     "image": "-", "present": True, "attested": True, "running": "-",
                     "problems": ["shared selector still set; remove it after per-service ones"]})
    return rows


def selectors(repo: Path) -> int:
    rows = selector_report(repo)
    for row in rows:
        state = "OK" if not row["problems"] else "PROBLEM: " + "; ".join(row["problems"])
        attested = (
            "attested" if row["attested"] else "LEGACY-UNATTESTED" if row["present"] else "-"
        )
        print(f"{row['service']} {row['variable']}={row['selector']} image={row['image']} "
              f"present={'yes' if row['present'] else 'NO'} {attested} "
              f"running={row['running']} {state}")
    return 1 if any(row["problems"] for row in rows) else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--sha", required=True)
    builder.add_argument("--services", default="web")
    checker = commands.add_parser("verify")
    checker.add_argument("--sha", required=True)
    checker.add_argument("--services", default="web")
    checker.add_argument("--runtime", action="store_true")
    commands.add_parser("selectors", help="read-only per-service selector and image report")
    args = parser.parse_args(argv)
    try:
        if args.command == "selectors":
            return selectors(args.repo.resolve())
        services = _services(args.services)
        if args.command == "build":
            build(args.repo.resolve(), args.sha, services)
        else:
            verify(args.repo.resolve(), args.sha, services, runtime=args.runtime)
    except IdentityError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
