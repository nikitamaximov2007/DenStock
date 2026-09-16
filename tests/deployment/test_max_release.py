"""MAX release tooling: gates, secret files, edge swap, role refresh, verify, plan.

Everything runs against a temporary root and a fake command runner; nothing
touches Docker, a server or a real secret.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))
sys.path.insert(0, str(ROOT))

import max_release as release  # noqa: E402

from tests.max_tls import make_local_ca  # noqa: E402

BASE = "ee89760d914511967adb54366052e59292b2a711"
CANDIDATE = "1" * 40
FAKE_TOKEN = "fake-max-token-for-release-tests"


class Runner:
    def __init__(self, responses=None):
        self.calls = []
        self.inputs = []
        self.responses = responses or {}

    def __call__(self, argv, input_text=None):
        self.calls.append(argv)
        self.inputs.append(input_text)
        text = " ".join(argv)
        for fragment, (code, stdout) in self.responses.items():
            if fragment in text:
                result = stdout(argv) if callable(stdout) else stdout
                return subprocess.CompletedProcess(argv, code, result, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def joined(self):
        return [" ".join(call) for call in self.calls]


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "opt"
    (root / "deploy/caddy").mkdir(parents=True)
    (root / "scripts/operations").mkdir(parents=True)
    for name in ("Caddyfile.production", "Caddyfile.production.pre-max"):
        shutil.copy(ROOT / "deploy/caddy" / name, root / "deploy/caddy" / name)
    shutil.copy(
        ROOT / "scripts/operations/create_public_catalog_role.sql",
        root / "scripts/operations/create_public_catalog_role.sql",
    )
    (root / ".env").write_text(
        f"DENSTOCK_APP_COMMIT={BASE}\n"
        "COMPOSE_FILE=docker-compose.yml:deploy/ai-support/docker-compose.external.yml:"
        "docker-compose.signing.yml\n"
    )
    (root / "docker-compose.signing.yml").write_text("services: {}\n")
    (root / ".env.public").write_text("PUBLIC_CATALOG_BASE_URL=https://pro-brp.ru\n")
    caddy = tmp_path / "etc/caddy/Caddyfile"
    caddy.parent.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/caddy/Caddyfile.production.pre-max", caddy)
    ca_dir = tmp_path / "etc/max"
    return {"root": root, "caddy": caddy, "ca_dir": ca_dir, "tmp": tmp_path}


def ctx_for(site, runner=None, *, execute=False, prompt=None):
    return release.Context(
        root=site["root"], caddyfile=site["caddy"], ca_dir=site["ca_dir"], execute=execute,
        require_root=False, run=runner or Runner(), prompt=prompt,
    )


def git_ok(head=BASE, status="?? docker-compose.signing.yml\n", ps="  1 /sbin/init\n"):
    return {
        "git rev-parse HEAD": (0, head + "\n"),
        "git status --porcelain": (0, status),
        "git cat-file -e": (0, ""),
        "ps -eo": (0, ps),
    }


# --- preflight ---------------------------------------------------------------------------


def test_preflight_passes_on_the_expected_production_state(site, capsys):
    report = release.preflight(ctx_for(site, Runner(git_ok())), expect_head=BASE,
                               candidate=CANDIDATE)
    assert report["caddyfile"] == "pre-max"
    assert report[".env.max"] == "absent" and report["ca_file"] == "absent"
    assert "PREFLIGHT PASS" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"git rev-parse HEAD": (0, "2" * 40 + "\n")}, "HEAD is"),
        ({"git status --porcelain": (0, " M docker-compose.yml\n")}, "not clean"),
        ({"ps -eo": (0, "4242 pg_dump -Fc denstock\n")}, "another writer"),
        ({"git cat-file -e": (1, "")}, "not fetched"),
    ],
)
def test_preflight_stops_on_each_failed_gate(site, change, reason, capsys):
    responses = {**git_ok(), **change}
    with pytest.raises(release.ReleaseError):
        release.preflight(ctx_for(site, Runner(responses)), expect_head=BASE, candidate=CANDIDATE)
    assert reason in capsys.readouterr().out


def test_preflight_stops_on_files_and_edge_state(site, capsys):
    (site["root"] / ".env").write_text(f"DENSTOCK_APP_COMMIT={'3' * 40}\nMAX_BOT_TOKEN=x\n")
    (site["root"] / "docker-compose.signing.yml").unlink()
    site["caddy"].write_text("something else\n")
    secret = site["root"] / ".env.max"
    secret.write_text("x")
    secret.chmod(0o644)
    with pytest.raises(release.ReleaseError):
        release.preflight(ctx_for(site, Runner(git_ok())), expect_head=BASE)
    out = capsys.readouterr().out
    for reason in (
        "DENSTOCK_APP_COMMIT does not match", "signing overlay", "MAX_BOT_TOKEN must not",
        "neither pre-max nor candidate", ".env.max must be mode 600",
    ):
        assert reason in out, reason


# --- install-secrets ---------------------------------------------------------------------


@pytest.fixture
def ca(site):
    local = make_local_ca(site["tmp"] / "ca-src")
    site["ca_dir"].mkdir(parents=True)
    shutil.copy(local.ca_file, site["ca_dir"] / release.CA_NAME)
    return local


def _prompt(*answers):
    values = iter(answers)
    return lambda _label: next(values)


def test_install_secrets_writes_two_private_files_and_prints_nothing_secret(site, ca, capsys):
    ctx = ctx_for(site, execute=True, prompt=_prompt(FAKE_TOKEN, FAKE_TOKEN))
    release.install_secrets(ctx, ca_sha256=ca.ca_sha256)
    max_file, hook = site["root"] / ".env.max", site["root"] / ".env.max-webhook"
    for path in (max_file, hook):
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    values = release.env_values(max_file)
    assert values["MAX_BOT_TOKEN"] == FAKE_TOKEN
    assert values["MAX_PUBLIC_WEBHOOK_URL"] == "https://pro-brp.ru/customer-requests/max/webhook/"
    assert values["MAX_API_CA_FILE"] == "/etc/denstock/max/russian-trusted-root-ca.pem"
    assert values["MAX_API_CA_SHA256"] == ca.ca_sha256
    hook_values = release.env_values(hook)
    assert hook_values["MAX_WEBHOOK_ENABLED"] == "true"
    assert release.SECRET_RE.fullmatch(hook_values["MAX_WEBHOOK_SECRET"])
    assert len(hook_values["MAX_WEBHOOK_SECRET"]) >= 60
    assert "MAX_WEBHOOK_SECRET" not in max_file.read_text()
    printed = capsys.readouterr().out + "\n".join(ctx.out)
    assert FAKE_TOKEN not in printed and hook_values["MAX_WEBHOOK_SECRET"] not in printed
    assert not list(site["root"].glob("*.partial"))


def test_install_secrets_dry_run_writes_nothing(site, ca):
    release.install_secrets(ctx_for(site), ca_sha256=ca.ca_sha256)
    assert not (site["root"] / ".env.max").exists()


@pytest.mark.parametrize(
    "answers", [(FAKE_TOKEN, "other"), ("", ""), ("has space", "has space")]
)
def test_install_secrets_refuses_bad_token_entries(site, ca, answers):
    ctx = ctx_for(site, execute=True, prompt=_prompt(*answers))
    with pytest.raises(release.ReleaseError):
        release.install_secrets(ctx, ca_sha256=ca.ca_sha256)
    assert not (site["root"] / ".env.max").exists()
    assert not (site["root"] / ".env.max-webhook").exists()


def test_install_secrets_checks_the_ca_before_asking_for_a_token(site, ca):
    asked = []
    ctx = ctx_for(site, execute=True, prompt=lambda label: asked.append(label) or FAKE_TOKEN)
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.install_secrets(ctx, ca_sha256="0" * 64)
    (site["ca_dir"] / release.CA_NAME).unlink()
    with pytest.raises(release.ReleaseError, match="place the official CA"):
        release.install_secrets(ctx, ca_sha256=ca.ca_sha256)
    with pytest.raises(release.ReleaseError, match="https"):
        release.install_secrets(ctx, ca_sha256=ca.ca_sha256, public_url="http://x/")
    assert asked == []


def test_install_secrets_never_overwrites_without_replace(site, ca):
    (site["root"] / ".env.max").write_text("MAX_BOT_TOKEN=keep\n")
    ctx = ctx_for(site, execute=True, prompt=_prompt(FAKE_TOKEN, FAKE_TOKEN))
    with pytest.raises(release.ReleaseError, match="--replace"):
        release.install_secrets(ctx, ca_sha256=ca.ca_sha256)
    assert "keep" in (site["root"] / ".env.max").read_text()


# --- username ----------------------------------------------------------------------------


def test_set_username_updates_env_public_in_place(site):
    path = site["root"] / ".env.public"
    path.write_text("PUBLIC_CATALOG_BASE_URL=https://pro-brp.ru\nMAX_BOT_USERNAME=old_bot\n")
    path.chmod(0o600)
    inode = path.stat().st_ino
    release.set_username(ctx_for(site), username="MAX_BOT_USERNAME=id123_bot")
    assert "old_bot" in path.read_text()  # dry run
    release.set_username(ctx_for(site, execute=True), username="MAX_BOT_USERNAME=id123_bot")
    assert path.read_text().splitlines() == [
        "PUBLIC_CATALOG_BASE_URL=https://pro-brp.ru", "MAX_BOT_USERNAME=id123_bot"
    ]
    assert path.stat().st_ino == inode and oct(path.stat().st_mode & 0o777) == "0o600"
    release.unset_username(ctx_for(site, execute=True))
    assert "MAX_BOT_USERNAME" not in path.read_text()


@pytest.mark.parametrize("bad", ["", "x", "bad name", "id_bot;rm -rf /", "a" * 70])
def test_set_username_refuses_unusable_values(site, bad):
    with pytest.raises(release.ReleaseError):
        release.set_username(ctx_for(site, execute=True), username=bad)


# --- edge --------------------------------------------------------------------------------


def _proxy_sees(site):
    return lambda argv: hashlib.sha256(site["caddy"].read_bytes()).hexdigest() + "  file\n"


def test_edge_install_swaps_in_place_validates_and_reloads(site):
    runner = Runner({"sha256sum": (0, _proxy_sees(site))})
    inode = site["caddy"].stat().st_ino
    release.edge_install(ctx_for(site, runner))
    pre_max = (ROOT / "deploy/caddy/Caddyfile.production.pre-max").read_text()
    assert site["caddy"].read_text() == pre_max
    release.edge_install(ctx_for(site, runner, execute=True))
    assert site["caddy"].read_text() == (ROOT / "deploy/caddy/Caddyfile.production").read_text()
    assert site["caddy"].stat().st_ino == inode
    calls = runner.joined()
    assert [c.split(" proxy ", 1)[1].split()[0] for c in calls] == ["sha256sum", "caddy", "caddy"]
    assert "caddy validate" in calls[1] and "caddy reload" in calls[2]
    backups = list(site["caddy"].parent.glob("Caddyfile.before-max-*"))
    assert len(backups) == 1
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == release.PRE_MAX_CADDY_SHA256
    # Repeating is a no-op.
    release.edge_install(ctx_for(site, Runner(), execute=True))


def test_edge_install_restores_the_previous_file_when_caddy_refuses(site):
    runner = Runner({"sha256sum": (0, _proxy_sees(site)), "caddy validate": (1, "")})
    with pytest.raises(release.ReleaseError):
        release.edge_install(ctx_for(site, runner, execute=True))
    assert hashlib.sha256(site["caddy"].read_bytes()).hexdigest() == release.PRE_MAX_CADDY_SHA256
    assert any("caddy reload" in call for call in runner.joined())


def test_edge_install_detects_a_container_that_does_not_see_the_file(site):
    runner = Runner({"sha256sum": (0, "0" * 64 + "  file\n")})
    with pytest.raises(release.ReleaseError, match="bind mount"):
        release.edge_install(ctx_for(site, runner, execute=True))
    assert hashlib.sha256(site["caddy"].read_bytes()).hexdigest() == release.PRE_MAX_CADDY_SHA256


def test_edge_install_refuses_an_unexpected_live_file(site):
    site["caddy"].write_text("# changed by someone\n")
    runner = Runner()
    with pytest.raises(release.ReleaseError, match="not the expected state"):
        release.edge_install(ctx_for(site, runner, execute=True))
    assert site["caddy"].read_text() == "# changed by someone\n"
    assert runner.calls == []


def test_edge_rollback_restores_the_recorded_production_file(site):
    runner = Runner({"sha256sum": (0, _proxy_sees(site))})
    release.edge_install(ctx_for(site, runner, execute=True))
    release.edge_rollback(ctx_for(site, runner, execute=True))
    assert hashlib.sha256(site["caddy"].read_bytes()).hexdigest() == release.PRE_MAX_CADDY_SHA256


# --- public role -------------------------------------------------------------------------


def test_public_role_runs_the_canonical_script_through_psql(site):
    runner = Runner()
    release.public_role(ctx_for(site, runner))
    assert runner.calls == []
    release.public_role(ctx_for(site, runner, execute=True))
    (call,) = runner.joined()
    assert "exec -T db sh -c psql -v ON_ERROR_STOP=1 -v public_role=denstock_public" in call
    assert runner.inputs[0] == (
        ROOT / "scripts/operations/create_public_catalog_role.sql"
    ).read_text()
    with pytest.raises(release.ReleaseError, match="unsafe"):
        release.public_role(ctx_for(site, runner, execute=True), role="x; drop table")


# --- verify ------------------------------------------------------------------------------

SHOWN = "\n".join(
    [" [X] 0008_max_messaging", " [X] 0009_max_public_link_guard", " [X] 0006_max_messaging"]
)


def _ps(services):
    import json

    return "\n".join(
        json.dumps({"Service": name, "State": "running", "Health": "healthy"}) for name in services
    )


def test_verify_passes_for_a_complete_rollout(site, capsys):
    (site["root"] / ".env").write_text(f"DENSTOCK_APP_COMMIT={CANDIDATE}\n")
    services = ["web", "telegram-bot", "max-bot", "catalog-web"]
    runner = Runner({
        "git rev-parse HEAD": (0, CANDIDATE),
        "showmigrations": (0, SHOWN),
        "ps --format json": (0, _ps(services)),
    })
    release.verify(ctx_for(site, runner), candidate=CANDIDATE, services=services,
                   require_subscribed=True)
    assert "VERIFY PASS" in capsys.readouterr().out
    joined = runner.joined()
    assert any("max_bot_health" in call for call in joined)
    assert any("max_webhook status --require-subscribed" in call for call in joined)
    assert sum("grep -q" in call for call in joined) == 4


def test_verify_names_every_failed_gate(site, capsys):
    (site["root"] / ".env").write_text(f"DENSTOCK_APP_COMMIT={BASE}\n")
    runner = Runner({
        "git rev-parse HEAD": (0, BASE),
        "migrate --check": (1, ""),
        "showmigrations": (0, " [ ] 0009_max_public_link_guard"),
        "ps --format json": (0, _ps(["web"])),
        "grep -q": (1, ""),
        "max_webhook status": (1, ""),
    })
    with pytest.raises(release.ReleaseError):
        release.verify(ctx_for(site, runner), candidate=CANDIDATE,
                       services=["web", "telegram-bot"], require_subscribed=True)
    out = capsys.readouterr().out
    for reason in ("is not the candidate", "unapplied migrations", "0009_max_public_link_guard",
                   "telegram-bot is None", "web does not run the candidate code",
                   "not subscribed"):
        assert reason in out, reason


# --- plan --------------------------------------------------------------------------------


def test_plan_orders_the_release_so_no_step_outruns_its_dependency():
    text = release.plan_text(base=BASE, candidate=CANDIDATE)

    def at(fragment):
        index = text.find(fragment)
        assert index >= 0, fragment
        return index

    order = [
        "preflight --expect-head",
        "denstock-backup-capped",
        "git checkout --detach " + CANDIDATE,
        "build web catalog-web telegram-bot max-bot",
        "install-secrets",
        "max-bot max_bot_identity   #",
        "up -d --no-deps --wait web",
        "public-role --execute",
        "--wait telegram-bot",
        "--wait max-bot",
        "edge-install --execute",
        "max_webhook subscribe --confirm",
        "max_webhook status --require-subscribed",
        "set-username",
        "--wait catalog-web",
        "--services web,telegram-bot,max-bot,catalog-web --require-subscribed",
        "POST backup",
    ]
    positions = [at(fragment) for fragment in order]
    assert positions == sorted(positions)
    rollback = text[at("ROLLBACK"):]
    steps = ["max_webhook unsubscribe", "stop max-bot", "unset-username", "edge-rollback",
             "git checkout --detach " + BASE]
    assert [rollback.find(step) for step in steps] == sorted(rollback.find(s) for s in steps)
    assert "restore the signed PRE backup" in rollback
    for secret_word in ("MAX_BOT_TOKEN=", "MAX_WEBHOOK_SECRET="):
        assert secret_word not in text


def test_cli_stops_with_a_safe_message(capsys, site):
    code = release.main(["--root", str(site["root"]), "plan", "--base", "nope", "--candidate", "x"])
    assert code == 1
    assert "STOP:" in capsys.readouterr().err


def test_the_tool_is_stdlib_only_and_executable():
    source = (ROOT / "scripts/operations/max_release.py").read_text()
    assert source.startswith("#!/usr/bin/python3")
    for module in ("django", "requests", "yaml"):
        assert f"import {module}" not in source
    assert os.access(ROOT / "scripts/operations/max_release.py", os.X_OK)
