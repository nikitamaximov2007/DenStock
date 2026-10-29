"""Fail-closed verification for image and runtime release identities."""

import json
import os
import shutil
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
        selector = (
            f"${{{identity.SELECTOR_VARS[service_name]}:-${{DENSTOCK_BUILD_REVISION:-unverified}}}}"
        )
        assert service["image"] == f"{identity.IMAGE_REFS[service_name]}:{selector}"
        assert build["context"] == "${DENSTOCK_BUILD_CONTEXT:-.}"
        assert build["args"]["DENSTOCK_BUILD_REVISION"] == selector
        assert build["args"]["DENSTOCK_BUILD_CONTEXT_SHA256"] == (
            "${DENSTOCK_BUILD_CONTEXT_SHA256:-unverified}"
        )
        assert service["environment"]["DENSTOCK_APP_COMMIT"] == selector


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
    (tmp_path / ".env").write_text(selector_env(web=SHA))

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
            selector_env(web=SHA) + f"DENSTOCK_APP_COMMIT={'f' * 40}\n"
        )
        with pytest.raises(identity.IdentityError, match="legacy"):
            identity.verify_runtime(tmp_path, "web", image_id, SHA)
    finally:
        identity.run = original


def test_predeployment_verify_rejects_legacy_env_before_image_inspection(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        selector_env(web=SHA) + f"DENSTOCK_APP_COMMIT={'f' * 40}\n"
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


OTHER = {"web": "1" * 40, "catalog-web": "2" * 40, "telegram-bot": "3" * 40, "max-bot": "4" * 40}


def selector_env(**overrides):
    values = dict(OTHER)
    values.update({key.replace("_", "-"): value for key, value in overrides.items()})
    return "".join(
        f"{variable}={values[service]}\n" for service, variable in identity.SELECTOR_VARS.items()
    )


def test_release_gate_checks_only_the_requested_service_selector(tmp_path):
    (tmp_path / ".env").write_text(selector_env(telegram_bot=SHA))
    identity.verify_release_env(tmp_path, SHA, ["telegram-bot"])
    for other in ("web", "catalog-web", "max-bot"):
        with pytest.raises(identity.IdentityError, match="does not match"):
            identity.verify_release_env(tmp_path, SHA, [other])


def test_release_gate_requires_an_own_selector_for_every_service(tmp_path):
    for missing in identity.SELECTOR_VARS.values():
        lines = [
            line for line in selector_env(web=SHA).splitlines() if not line.startswith(missing)
        ]
        (tmp_path / ".env").write_text("\n".join(lines) + "\n")
        with pytest.raises(identity.IdentityError, match="no own full-SHA selector"):
            identity.verify_release_env(tmp_path, SHA, ["web"])
    (tmp_path / ".env").write_text(selector_env(web=SHA).replace(OTHER["max-bot"], "latest"))
    with pytest.raises(identity.IdentityError, match="no own full-SHA selector"):
        identity.verify_release_env(tmp_path, SHA, ["web"])


def test_release_gate_rejects_the_shared_selector(tmp_path):
    (tmp_path / ".env").write_text(selector_env(web=SHA) + f"DENSTOCK_BUILD_REVISION={SHA}\n")
    with pytest.raises(identity.IdentityError, match="shared DENSTOCK_BUILD_REVISION"):
        identity.verify_release_env(tmp_path, SHA, ["web"])


def test_build_points_only_the_requested_service_at_the_new_commit(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(identity, "resolve_commit", lambda repo, requested: SHA)
    monkeypatch.setattr(identity, "source_archive", lambda repo, commit, staging: ARCHIVE_SHA)

    def fake_checked(argv, *, cwd=ROOT, env=None):
        seen["argv"], seen["env"] = argv, env
        return b""

    monkeypatch.setattr(identity, "checked", fake_checked)
    monkeypatch.setattr(identity, "compose_images", lambda repo, services, commit: {
        service: "sha256:" + "c" * 64 for service in services
    })
    monkeypatch.setattr(identity, "inspect_image", lambda image_id: image_metadata())
    monkeypatch.setenv("DENSTOCK_BUILD_REVISION", "f" * 40)
    monkeypatch.setenv("DENSTOCK_WEB_REVISION", "e" * 40)
    identity.build(tmp_path, SHA, ["max-bot"])
    assert seen["argv"] == ["docker", "compose", "build", "max-bot"]
    assert seen["env"]["DENSTOCK_MAX_BOT_REVISION"] == SHA
    assert "DENSTOCK_BUILD_REVISION" not in seen["env"]
    assert seen["env"]["DENSTOCK_WEB_REVISION"] == "e" * 40


def test_selector_report_flags_missing_images_and_marks_legacy_ones(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(selector_env())
    monkeypatch.setattr(identity, "compose_service_images", lambda repo: {
        service: f"{identity.IMAGE_REFS[service]}:{OTHER[service]}" for service in OTHER
    })
    present = {
        f"denstock-web:{OTHER['web']}": image_metadata(revision=OTHER["web"]),
        f"denstock-telegram-bot:{OTHER['telegram-bot']}": image_metadata(revision=""),
        f"denstock-max-bot:{OTHER['max-bot']}": image_metadata(revision=OTHER["max-bot"]),
    }

    def fake_run(argv, *, cwd=ROOT, env=None):
        if argv[:3] == ["docker", "image", "inspect"] and argv[3] in present:
            return subprocess.CompletedProcess(argv, 0, json_bytes([present[argv[3]]]), b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(identity, "run", fake_run)
    monkeypatch.setattr(identity, "running_image_id", lambda repo, service: "")
    rows = {row["service"]: row for row in identity.selector_report(tmp_path)}
    assert rows["web"]["present"] and rows["web"]["attested"] and not rows["web"]["problems"]
    assert rows["telegram-bot"]["present"] and not rows["telegram-bot"]["attested"]
    assert not rows["telegram-bot"]["problems"]
    assert rows["catalog-web"]["problems"] == ["selected image does not exist"]
    assert identity.selectors(tmp_path) == 1


def test_selector_report_flags_an_overlay_that_overrides_the_selector(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(selector_env())
    resolved = {service: f"{identity.IMAGE_REFS[service]}:{OTHER[service]}" for service in OTHER}
    resolved["catalog-web"] = "denstock-catalog-web:" + "8" * 40
    monkeypatch.setattr(identity, "compose_service_images", lambda repo: resolved)
    monkeypatch.setattr(identity, "run", lambda argv, *, cwd=ROOT, env=None: (
        subprocess.CompletedProcess(argv, 0, json_bytes([image_metadata()]), b"")))
    monkeypatch.setattr(identity, "running_image_id", lambda repo, service: "")
    rows = {row["service"]: row for row in identity.selector_report(tmp_path)}
    assert "overlay" in rows["catalog-web"]["problems"][0]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker compose is not installed")
def test_compose_render_moves_only_the_service_whose_selector_changed(tmp_path):
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    for name in (".env.public", ".env.telegram", ".env.max", ".env.max-webhook"):
        (tmp_path / name).write_text("")

    def render(selectors):
        (tmp_path / ".env").write_text("".join(
            f"{identity.SELECTOR_VARS[service]}={sha}\n" for service, sha in selectors.items()
        ) + "DENSTOCK_BUILD_REVISION=" + "9" * 40 + "\n")
        result = subprocess.run(
            ["docker", "compose", "--profile", "public-catalog", "--profile", "telegram-bot",
             "--profile", "max-bot", "config", "--format", "json"],
            cwd=tmp_path, capture_output=True, text=True,
            env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(tmp_path))},
        )
        if result.returncode:
            pytest.skip("docker compose config unavailable: " + result.stderr[:200])
        return json.loads(result.stdout)["services"]

    before = render(OTHER)
    for service, sha in OTHER.items():
        rendered = before[service]
        assert rendered["image"] == f"{identity.IMAGE_REFS[service]}:{sha}"
        assert rendered["environment"]["DENSTOCK_APP_COMMIT"] == sha
        assert rendered["build"]["args"]["DENSTOCK_BUILD_REVISION"] == sha
    after = render({**OTHER, "web": SHA})
    # Every service reads the whole .env through env_file, so the public selector
    # values themselves are visible in each container. Apart from them nothing
    # of another service may change: image, commit, build args, mounts, command.
    selector_names = set(identity.SELECTOR_VARS.values())

    def without_selectors(service):
        service = json.loads(json.dumps(service))
        for name in selector_names:
            service.get("environment", {}).pop(name, None)
        return service

    for service in OTHER:
        if service == "web":
            assert after[service]["image"] == f"denstock-web:{SHA}"
            assert after[service]["environment"]["DENSTOCK_APP_COMMIT"] == SHA
            assert after[service]["build"]["args"]["DENSTOCK_BUILD_REVISION"] == SHA
        else:
            assert after[service]["image"] == before[service]["image"]
            assert without_selectors(after[service]) == without_selectors(before[service]), service
    for name in ("db", "proxy"):
        assert without_selectors(after[name]) == without_selectors(before[name])
