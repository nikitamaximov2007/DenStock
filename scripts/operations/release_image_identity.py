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
    env_file = repo / ".env"
    if not env_file.is_file():
        raise IdentityError("runtime .env file is missing")
    configured = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key:
            configured[key] = value.strip().strip("'\"")
    if "DENSTOCK_APP_COMMIT" in configured:
        raise IdentityError("legacy DENSTOCK_APP_COMMIT override remains in .env")
    if configured.get("DENSTOCK_BUILD_REVISION") != commit:
        raise IdentityError(".env image selector does not match the verified release SHA")
    raw = checked(["docker", "compose", "ps", "-q", service], cwd=repo)
    containers = [line.strip() for line in raw.decode().splitlines() if line.strip()]
    if len(containers) != 1:
        raise IdentityError(f"expected exactly one running container for {service}")
    container = inspect_container(containers[0])
    if container.get("Image") != image_id:
        raise IdentityError(f"running {service} image ID differs from the verified image")
    if _env_map(container.get("Config", {}).get("Env")).get("DENSTOCK_APP_COMMIT") != commit:
        raise IdentityError(f"running {service} environment differs from the verified image SHA")


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
            "DENSTOCK_BUILD_REVISION": commit,
            "DENSTOCK_BUILD_CONTEXT_SHA256": archive_sha256,
        })
        checked(["docker", "compose", "build", *services], cwd=repo, env=env)
        image_ids = compose_images(repo, services, commit)
        for service in services:
            image = inspect_image(image_ids[service])
            verify_image(image, commit=commit, archive_sha256=archive_sha256)
            print(f"BUILD IDENTITY PASS service={service} sha={commit} image={image_ids[service]} "
                  f"source_archive_sha256={archive_sha256}")


def verify(repo: Path, requested: str, services: list[str], *, runtime: bool) -> None:
    commit = resolve_commit(repo, requested)
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
    args = parser.parse_args(argv)
    try:
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
