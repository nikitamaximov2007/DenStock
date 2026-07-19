import ast
import io
import logging
import os
import sys
import uuid
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.core.checks import Tags, run_checks
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image, ImageDraw

from apps.accounts import roles
from apps.ai_support.contracts import AUDITED_CODEX_CLI_VERSION
from apps.ai_support.diagnostics import (
    canonical_public_url,
    safe_diagnostic_snapshot,
    safe_route_context,
)
from apps.ai_support.knowledge import retrieve
from apps.ai_support.models import SupportConversation
from apps.ai_support.providers.external_launcher import ExternalLauncherError
from apps.ai_support.services import send_message
from apps.inventory.models import StockBalance, StockMovement
from apps.receipts.models import Receipt
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale

FORBIDDEN_SERVICE_MODULES = {
    "apps.actions.services",
    "apps.catalog.services",
    "apps.inventory.services",
    "apps.receipts.services",
    "apps.repairs.services",
    "apps.returns.services",
    "apps.sales.services",
    "apps.stocktaking.services",
    "apps.warehouse.services",
    "apps.writeoffs.services",
}


def configure_codex_security_check(settings, tmp_path, *, provider="codex_cli"):
    home = tmp_path / "codex-home"
    workspace = tmp_path / "runtime"
    home.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    if os.name != "nt":
        home.chmod(0o700)
        workspace.chmod(0o700)
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = provider
    settings.DEBUG = True
    settings.AI_SUPPORT_CODEX_BINARY = sys.executable
    settings.AI_SUPPORT_CODEX_REQUIRED_VERSION = AUDITED_CODEX_CLI_VERSION
    settings.AI_SUPPORT_CODEX_MODEL = "configured-model"
    settings.AI_SUPPORT_CODEX_HOME = str(home)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = 1
    settings.AI_SUPPORT_CODEX_LAUNCH_MODE = "direct_dev"
    settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION = True


class BrokenProviderName:
    def __init__(self):
        self.string_calls = 0

    def __str__(self):
        self.string_calls += 1
        raise RuntimeError("provider normalization failed")


def test_ai_support_has_no_mutation_service_sql_or_url_fetch_imports(settings):
    root = Path(settings.BASE_DIR) / "apps" / "ai_support"
    imported = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
    assert not (imported & FORBIDDEN_SERVICE_MODULES)
    assert not ({"urllib.request", "requests"} & imported)
    socket_importers = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        if "socket" in modules:
            socket_importers.append(path.relative_to(root).as_posix())
    assert socket_importers == ["providers/external_launcher.py"]
    subprocess_importers = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        if "subprocess" in modules:
            subprocess_importers.append(path.relative_to(root).as_posix())
    assert subprocess_importers == ["providers/codex_cli.py"]
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    assert "cursor.execute" not in source
    assert "os.system" not in source
    assert "tools=" not in source
    assert "shell=True" not in source
    assert "danger-full-access" not in source
    assert "workspace-write" not in source
    assert "--search" not in source
    assert "--yolo" not in source
    assert "pending +=" not in source


def test_api_sdk_and_key_configuration_are_absent(settings):
    root = Path(settings.BASE_DIR)
    package = root / "apps" / "ai_support"
    source = "\n".join(path.read_text(encoding="utf-8") for path in package.rglob("*.py"))
    forbidden = (
        "import " + "openai",
        "from " + "openai",
        "OpenAI" + "Provider",
        "responses" + ".create",
        "AI_SUPPORT_" + "API_KEY",
    )
    assert not any(value in source for value in forbidden)


def test_codex_runtime_security_check_requires_isolated_paths(settings, tmp_path):
    home = tmp_path / "codex-home"
    workspace = tmp_path / "runtime"
    home.mkdir()
    workspace.mkdir()
    if os.name != "nt":
        home.chmod(0o700)
        workspace.chmod(0o700)
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "codex_cli"
    settings.DEBUG = True
    settings.AI_SUPPORT_CODEX_BINARY = sys.executable
    settings.AI_SUPPORT_CODEX_REQUIRED_VERSION = "0.142.5"
    settings.AI_SUPPORT_CODEX_MODEL = "configured-model"
    settings.AI_SUPPORT_CODEX_HOME = str(home)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = 1
    settings.AI_SUPPORT_CODEX_LAUNCH_MODE = "direct_dev"
    settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION = True
    assert not {
        error.id for error in run_checks(tags=[Tags.security])
    } & {f"ai_support.E{number:03d}" for number in range(2, 16)}
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(Path(settings.BASE_DIR) / "runtime")
    assert "ai_support.E006" in {error.id for error in run_checks(tags=[Tags.security])}


@pytest.mark.parametrize("provider", ["codex_cli", "CODEX_CLI", "Codex_Cli", " codex_cli "])
def test_codex_provider_variants_run_the_same_security_checks(settings, tmp_path, provider):
    configure_codex_security_check(settings, tmp_path, provider=provider)
    settings.AI_SUPPORT_CODEX_REQUIRED_VERSION = ""
    assert "ai_support.E008" in {error.id for error in run_checks(tags=[Tags.security])}


@pytest.mark.parametrize("required_version", ["0.142.4", "0.142.6", "0.143.0", "", "bad"])
def test_security_check_rejects_every_unaudited_required_version(
    settings, tmp_path, required_version
):
    configure_codex_security_check(settings, tmp_path)
    settings.AI_SUPPORT_CODEX_REQUIRED_VERSION = required_version
    errors = [error for error in run_checks(tags=[Tags.security]) if error.id == "ai_support.E008"]
    assert len(errors) == 1
    assert "security audit" in errors[0].msg or "pinned semantic version" in errors[0].msg


@pytest.mark.parametrize(
    "provider",
    ["codex_cli", "CODEX_CLI", " codex_cli ", "fake", "disabled", "unknown", ""],
)
def test_production_ai_support_is_always_rejected(settings, tmp_path, provider):
    configure_codex_security_check(settings, tmp_path, provider=provider)
    settings.DEBUG = False
    errors = [error for error in run_checks(tags=[Tags.security]) if error.id == "ai_support.E015"]
    assert len(errors) == 1


@pytest.mark.parametrize("provider", [None, object()])
def test_production_guard_rejects_non_string_provider_without_normalizing(
    settings, tmp_path, provider
):
    configure_codex_security_check(settings, tmp_path, provider=provider)
    settings.DEBUG = False
    assert {error.id for error in run_checks(tags=[Tags.security])} == {
        "ai_support.E015"
    }


def test_production_guard_precedes_broken_provider_normalization(settings, tmp_path):
    provider = BrokenProviderName()
    configure_codex_security_check(settings, tmp_path, provider=provider)
    settings.DEBUG = False
    assert {error.id for error in run_checks(tags=[Tags.security])} == {
        "ai_support.E015"
    }
    assert provider.string_calls == 0


def test_debug_broken_provider_fails_closed(settings, tmp_path):
    provider = BrokenProviderName()
    configure_codex_security_check(settings, tmp_path, provider=provider)
    errors = run_checks(tags=[Tags.security])
    assert {error.id for error in errors} == {"ai_support.E014"}
    assert provider.string_calls == 1


def test_production_guard_does_not_run_when_feature_is_disabled(settings, tmp_path):
    configure_codex_security_check(settings, tmp_path)
    settings.AI_SUPPORT_ENABLED = False
    settings.DEBUG = False
    assert "ai_support.E015" not in {error.id for error in run_checks(tags=[Tags.security])}


def test_disabled_feature_does_not_normalize_provider(settings, tmp_path):
    provider = BrokenProviderName()
    configure_codex_security_check(settings, tmp_path, provider=provider)
    settings.AI_SUPPORT_ENABLED = False
    assert run_checks(tags=[Tags.security]) == []
    assert provider.string_calls == 0


def test_debug_enabled_continues_with_provider_specific_checks(settings, tmp_path):
    configure_codex_security_check(settings, tmp_path, provider="fake")
    settings.DEBUG = True
    settings.AI_SUPPORT_CODEX_REQUIRED_VERSION = ""
    error_ids = {error.id for error in run_checks(tags=[Tags.security])}
    assert "ai_support.E015" not in error_ids
    assert "ai_support.E008" not in error_ids


def test_external_mode_requires_the_fixed_linux_socket(settings, tmp_path):
    configure_codex_security_check(settings, tmp_path)
    settings.AI_SUPPORT_CODEX_LAUNCH_MODE = "external"
    settings.AI_SUPPORT_CODEX_LAUNCHER_SOCKET = str(tmp_path / "launcher.sock")
    settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION = False
    assert "ai_support.E011" in {error.id for error in run_checks(tags=[Tags.security])}


def configure_external_security_check(settings, tmp_path):
    workspace = tmp_path / "requests"
    workspace.mkdir(exist_ok=True)
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "codex_cli"
    settings.DEBUG = False
    settings.AI_SUPPORT_CODEX_REQUIRED_VERSION = AUDITED_CODEX_CLI_VERSION
    settings.AI_SUPPORT_CODEX_MODEL = "configured-model"
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = 1
    settings.AI_SUPPORT_CODEX_LAUNCH_MODE = "external"
    settings.AI_SUPPORT_CODEX_LAUNCHER_SOCKET = "/run/denstock-ai/launcher.sock"
    settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION = False


def test_production_external_mode_requires_successful_handshake(
    settings, tmp_path, monkeypatch
):
    configure_external_security_check(settings, tmp_path)
    monkeypatch.setattr("apps.ai_support.checks._is_posix_platform", lambda: True)
    monkeypatch.setattr("apps.ai_support.checks.validate_launcher_socket", lambda path: path)
    monkeypatch.setattr("apps.ai_support.checks._external_workspace_errors", lambda path: [])
    monkeypatch.setattr(
        "apps.ai_support.checks.query_launcher_ready",
        lambda *args, **kwargs: {"proxy_health": "ok"},
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    error_ids = {error.id for error in run_checks(tags=[Tags.security])}

    assert not error_ids & {"ai_support.E011", "ai_support.E015", "ai_support.E016"}


def test_production_external_mode_fails_closed_on_launcher_error(
    settings, tmp_path, monkeypatch
):
    configure_external_security_check(settings, tmp_path)
    monkeypatch.setattr("apps.ai_support.checks._is_posix_platform", lambda: True)
    monkeypatch.setattr("apps.ai_support.checks.validate_launcher_socket", lambda path: path)
    monkeypatch.setattr("apps.ai_support.checks._external_workspace_errors", lambda path: [])

    def fail(*_args, **_kwargs):
        raise ExternalLauncherError("provider_unavailable")

    monkeypatch.setattr("apps.ai_support.checks.query_launcher_ready", fail)
    assert "ai_support.E015" in {error.id for error in run_checks(tags=[Tags.security])}


def test_production_external_mode_rejects_api_key_environment(
    settings, tmp_path, monkeypatch
):
    configure_external_security_check(settings, tmp_path)
    monkeypatch.setattr("apps.ai_support.checks._is_posix_platform", lambda: True)
    monkeypatch.setattr("apps.ai_support.checks._external_workspace_errors", lambda path: [])
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    assert "ai_support.E016" in {error.id for error in run_checks(tags=[Tags.security])}


def test_direct_dev_requires_explicit_opt_in(settings, tmp_path):
    configure_codex_security_check(settings, tmp_path)
    settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION = False
    assert "ai_support.E011" in {error.id for error in run_checks(tags=[Tags.security])}


def test_codex_security_checks_pin_binary_concurrency_and_launcher(settings, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    if os.name != "nt":
        home.chmod(0o700)
        workspace.chmod(0o700)
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "codex_cli"
    settings.AI_SUPPORT_CODEX_MODEL = "model"
    settings.AI_SUPPORT_CODEX_HOME = str(home)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    settings.AI_SUPPORT_CODEX_RUNTIME_RETENTION_HOURS = 24
    settings.AI_SUPPORT_CODEX_BINARY = str(tmp_path / "missing-codex")
    settings.AI_SUPPORT_CODEX_REQUIRED_VERSION = ""
    settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = 2
    settings.AI_SUPPORT_CODEX_LAUNCH_MODE = "direct_dev"
    settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION = False
    settings.DEBUG = True
    error_ids = {error.id for error in run_checks(tags=[Tags.security])}
    assert {"ai_support.E008", "ai_support.E009", "ai_support.E010", "ai_support.E011"} <= error_ids


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation is privilege-dependent on Windows")
def test_codex_security_check_rejects_symlinked_runtime(settings, tmp_path):
    real_home = tmp_path / "real-home"
    workspace = tmp_path / "workspace"
    real_home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "codex_cli"
    settings.DEBUG = True
    settings.AI_SUPPORT_CODEX_BINARY = sys.executable
    settings.AI_SUPPORT_CODEX_REQUIRED_VERSION = "0.142.5"
    settings.AI_SUPPORT_CODEX_MODEL = "model"
    settings.AI_SUPPORT_CODEX_HOME = str(linked_home)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY = 1
    settings.AI_SUPPORT_CODEX_LAUNCH_MODE = "direct_dev"
    settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION = True
    assert "ai_support.E012" in {error.id for error in run_checks(tags=[Tags.security])}


@pytest.mark.parametrize(
    "path",
    [
        "https://example.invalid/search/",
        "//example.invalid/search/",
        "/search/?secret=1",
        "/search/#fragment",
        "/admin/",
        "/missing/",
        "\\server\\share",
    ],
)
def test_route_context_rejects_malformed_or_unapproved_values(path):
    assert safe_route_context(path) == {}


def test_route_context_and_diagnostics_only_keep_allowlisted_fields(
    db, django_user_model, settings
):
    user = django_user_model.objects.create_user(username="diagnostic")
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    settings.DENSTOCK_PUBLIC_BASE_URL = "https://warehouse.example/"
    settings.DENSTOCK_APP_COMMIT = "a" * 40
    route = safe_route_context(reverse("part_search"))
    snapshot = safe_diagnostic_snapshot(
        user=user, route_context=route, browser_family="Chrome", viewport="1280x720"
    )
    assert snapshot == {
        "path": "/search/",
        "route_name": "part_search",
        "roles": [roles.STOREKEEPER],
        "browser_family": "Chrome",
        "viewport": "1280x720",
        "app_commit": "a" * 40,
        "public_base_url": "https://warehouse.example/",
    }
    assert not ({"cookies", "headers", "query", "email", "environment"} & snapshot.keys())


@pytest.mark.parametrize(
    "url",
    ["", "javascript:alert(1)", "https://user:pass@example.com/", "https://example.com/?x=1"],
)
def test_invalid_public_base_url_is_not_exposed(settings, url):
    settings.DENSTOCK_PUBLIC_BASE_URL = url
    assert canonical_public_url() == ""


def screenshot():
    output = io.BytesIO()
    image = Image.new("RGB", (260, 40), "white")
    ImageDraw.Draw(image).text((2, 10), "https://185.250.44.206/", fill="black")
    image.save(output, "PNG")
    return SimpleUploadedFile("error.png", output.getvalue(), content_type="image/png")


def test_https_playbook_integration_with_fake_provider_is_read_only(
    db, django_user_model, settings, tmp_path
):
    user = django_user_model.objects.create_user(username="https-user")
    user.groups.add(Group.objects.get(name=roles.SELLER))
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "fake"
    settings.AI_SUPPORT_ALLOW_FAKE_PROVIDER = True
    settings.AI_SUPPORT_CODEX_MODEL = "fake"
    settings.DENSTOCK_PUBLIC_BASE_URL = "https://185-250-44-206.sslip.io/"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    conversation = SupportConversation.objects.create(owner=user)
    before = {
        "movements": StockMovement.objects.count(),
        "balances": StockBalance.objects.count(),
        "sales": Sale.objects.count(),
        "repairs": RepairOrder.objects.count(),
        "receipts": Receipt.objects.count(),
    }
    assert retrieve("После продажи ERR_SSL_PROTOCOL_ERROR")[0].source_id == "https-canonical-url"
    result = send_message(
        conversation=conversation,
        user=user,
        text="После проведения продажи появилось ERR_SSL_PROTOCOL_ERROR",
        token=uuid.uuid4(),
        route_path=reverse("sale_list"),
        upload=screenshot(),
        image_consent=True,
    )
    answer = result.assistant_message.text
    assert "голому IP" in answer
    assert "https://185-250-44-206.sslip.io/" in answer
    assert "Не нажимайте «Провести продажу» повторно" in answer
    for phrase in ("список продаж", "проведена", "остатки", "дубля"):
        assert phrase in answer
    assert result.user_message.attachment.shared_with_provider_at is not None
    after = {
        "movements": StockMovement.objects.count(),
        "balances": StockBalance.objects.count(),
        "sales": Sale.objects.count(),
        "repairs": RepairOrder.objects.count(),
        "receipts": Receipt.objects.count(),
    }
    assert after == before


def test_ai_logs_exclude_message_image_and_system_prompt(
    caplog, db, django_user_model, settings, tmp_path
):
    user = django_user_model.objects.create_user(username="log-user")
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "fake"
    settings.AI_SUPPORT_ALLOW_FAKE_PROVIDER = True
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    conversation = SupportConversation.objects.create(owner=user)
    secret_text = "MESSAGE_SECRET_SHOULD_NOT_BE_LOGGED"
    with caplog.at_level(logging.INFO, logger="denstock.ai_support"):
        send_message(
            conversation=conversation,
            user=user,
            text=secret_text,
            token=uuid.uuid4(),
        )
    logs = caplog.text
    assert secret_text not in logs
    assert "ДОВЕРЕННЫЕ СИСТЕМНЫЕ ПРАВИЛА" not in logs
    assert "ai_support_request" in logs


def test_partial_navigation_hook_and_ui_states_exist(settings):
    root = Path(settings.BASE_DIR)
    script = (root / "static" / "js" / "ai_support.js").read_text(encoding="utf-8")
    template = (root / "templates" / "ai_support" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "denstock:page-loaded" in script
    assert "data-support-history-toggle" in template
    for text in ("ИИ может ошибаться", "выключена", "недоступен", "Отправка..."):
        assert text in template
    assert "data-error-code" in template
    assert "overflow-wrap" in (root / "static" / "css" / "app.css").read_text(
        encoding="utf-8"
    )
