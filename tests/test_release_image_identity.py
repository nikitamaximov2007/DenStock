"""Fail-closed verification for image and runtime release identities."""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import release_image_identity as identity  # noqa: E402

SHA = "a" * 40
ARCHIVE_SHA = "b" * 64


def image_metadata(*, revision=SHA, archive_sha=ARCHIVE_SHA, env_sha=SHA):
    return {
        "Id": "sha256:" + "c" * 64,
        "Config": {
            "Labels": {
                identity.REVISION_LABEL: revision,
                identity.ARCHIVE_SHA_LABEL: archive_sha,
            },
            "Env": [f"DENSTOCK_APP_COMMIT={env_sha}", "DJANGO_SECRET_KEY=not-for-logs"],
        },
    }


def test_image_identity_requires_revision_archive_and_environment_to_match():
    identity.verify_image(image_metadata(), commit=SHA, archive_sha256=ARCHIVE_SHA)
    for candidate in (
        image_metadata(revision="d" * 40),
        image_metadata(archive_sha="e" * 64),
        image_metadata(env_sha="f" * 40),
    ):
        with pytest.raises(identity.IdentityError):
            identity.verify_image(candidate, commit=SHA, archive_sha256=ARCHIVE_SHA)


def test_compose_builds_all_application_services_from_verified_context():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for service_name in ("web", "catalog-web", "telegram-bot", "max-bot"):
        service = compose["services"][service_name]
        build = service["build"]
        expected_image = (
            f"{identity.IMAGE_REFS[service_name]}:${{DENSTOCK_BUILD_REVISION:-unverified}}"
        )
        assert service["image"] == expected_image
        assert build["context"] == "${DENSTOCK_BUILD_CONTEXT:-.}"
        assert build["args"]["DENSTOCK_BUILD_REVISION"] == (
            "${DENSTOCK_BUILD_REVISION:-unverified}"
        )
        assert build["args"]["DENSTOCK_BUILD_CONTEXT_SHA256"] == (
            "${DENSTOCK_BUILD_CONTEXT_SHA256:-unverified}"
        )
        assert service["environment"]["DENSTOCK_APP_COMMIT"] == (
            "${DENSTOCK_BUILD_REVISION:-unverified}"
        )


def test_dockerfile_contains_only_public_revision_attestation_fields():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    assert identity.REVISION_LABEL in dockerfile
    assert identity.ARCHIVE_SHA_LABEL in dockerfile
    assert "DENSTOCK_APP_COMMIT=${DENSTOCK_BUILD_REVISION}" in dockerfile
    assert "SECRET" not in dockerfile.upper()


def test_resolve_commit_rejects_worktree_and_control_file_mismatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Synthetic Test"],
                   check=True)
    for relative in identity.CONTROL_FILES:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    subprocess.run(["git", "-C", str(repo), "add", *identity.CONTROL_FILES], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "synthetic release source"],
                   check=True)
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    assert identity.resolve_commit(repo, commit) == commit
    with tempfile.TemporaryDirectory() as temporary:
        archive_sha = identity.source_archive(repo, commit, Path(temporary))
        assert len(archive_sha) == 64
        assert (Path(temporary) / "context" / "docker-compose.yml").is_file()
    (repo / "docker" / "Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(identity.IdentityError, match="control file differs"):
        identity.resolve_commit(repo, commit)


def test_release_command_errors_do_not_echo_docker_environment(monkeypatch, capsys):
    secret = "synthetic-never-print-this"

    def fail_run(argv, *, cwd=ROOT, env=None):
        return subprocess.CompletedProcess(argv, 1, b"", secret.encode())

    monkeypatch.setattr(identity, "run", fail_run)
    with pytest.raises(identity.IdentityError, match="command failed"):
        identity.checked(["docker", "image", "inspect", "sha256:test"])
    assert secret not in capsys.readouterr().out


def test_runtime_gate_requires_new_selector_and_rejects_legacy_override(tmp_path):
    image_id = "sha256:" + "c" * 64
    (tmp_path / ".env").write_text(f"DENSTOCK_BUILD_REVISION={SHA}\n")

    class Runner:
        def __call__(self, argv, *, cwd=ROOT, env=None):
            if argv[:4] == ["docker", "compose", "ps", "-q"]:
                return subprocess.CompletedProcess(argv, 0, b"container-1\n", b"")
            if argv[:3] == ["docker", "container", "inspect"]:
                payload = [{"Image": image_id, "Config": {"Env": [f"DENSTOCK_APP_COMMIT={SHA}"]}}]
                return subprocess.CompletedProcess(argv, 0, json_bytes(payload), b"")
            return subprocess.CompletedProcess(argv, 1, b"", b"")

    original = identity.run
    try:
        identity.run = Runner()
        identity.verify_runtime(tmp_path, "web", image_id, SHA)
        (tmp_path / ".env").write_text(
            f"DENSTOCK_BUILD_REVISION={SHA}\nDENSTOCK_APP_COMMIT={'f' * 40}\n"
        )
        with pytest.raises(identity.IdentityError, match="legacy"):
            identity.verify_runtime(tmp_path, "web", image_id, SHA)
    finally:
        identity.run = original


def test_predeployment_verify_rejects_legacy_env_before_image_inspection(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        f"DENSTOCK_BUILD_REVISION={SHA}\nDENSTOCK_APP_COMMIT={'f' * 40}\n"
    )
    monkeypatch.setattr(identity, "resolve_commit", lambda repo, requested: SHA)
    monkeypatch.setattr(
        identity,
        "checked",
        lambda *args, **kwargs: pytest.fail("image inspection must not run with stale .env"),
    )

    with pytest.raises(identity.IdentityError, match="legacy"):
        identity.verify(tmp_path, SHA, ["web"], runtime=False)


def json_bytes(value):
    import json

    return json.dumps(value).encode()
