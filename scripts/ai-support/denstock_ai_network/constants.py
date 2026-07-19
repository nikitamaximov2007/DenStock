from pathlib import Path

PROTOCOL_VERSION = 1
LAUNCHER_VERSION = "1.0.0"
CODEX_CLI_VERSION = "0.142.5"
CODEX_ARCHIVE_NAME = "codex-x86_64-unknown-linux-musl.tar.gz"
CODEX_ARCHIVE_SHA256 = "cb933ec3cb61bf4b5fc88eecf5e6149829faa6172535b6ef0afb0154beb4aab8"
CODEX_ARCHIVE_URL = (
    "https://github.com/openai/codex/releases/download/rust-v0.142.5/"
    f"{CODEX_ARCHIVE_NAME}"
)
CODEX_ARCHIVE_MEMBER = "codex-x86_64-unknown-linux-musl"
CODEX_BINARY_SHA256 = "ac06f492f3ded7a8e2f36dc961e3cc5276a3c4841a2695d4681d0557c5b30e41"
CODEX_BINARY_BYTES = 285_929_520
SING_BOX_VERSION = "1.13.14"
SING_BOX_DEB_NAME = "sing-box_1.13.14_linux_amd64.deb"
SING_BOX_DEB_SHA256 = "320523f9586877c4cb244df753d848356787e15f2f4e23a00908af2422206542"
SING_BOX_DEB_URL = (
    "https://github.com/SagerNet/sing-box/releases/download/v1.13.14/"
    f"{SING_BOX_DEB_NAME}"
)

CONFIG_ROOT = Path("/etc/denstock-ai")
LAUNCHER_CONFIG_PATH = CONFIG_ROOT / "launcher.json"
MAXINIK_ENV_PATH = CONFIG_ROOT / "maxinik.env"
SING_BOX_CONFIG_PATH = CONFIG_ROOT / "sing-box.json"
NFTABLES_CONFIG_PATH = Path("/run/denstock-ai/nftables.conf")

AI_USER = "denstock-ai"
PROXY_USER = "denstock-ai-proxy"
CLIENT_GROUP = "denstock-ai-client"
FIREWALL_TABLE = "denstock_ai"
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 2080
REQUEST_ROOT_MODE = 0o1731

HEALTH_STATUSES = frozenset(
    {
        "ok",
        "proxy_unavailable",
        "direct_network_not_blocked",
        "unexpected_egress",
        "configuration_error",
    }
)

DISABLED_CODEX_FEATURES = (
    "apply_patch_streaming_events",
    "apps",
    "artifact",
    "auth_elicitation",
    "auto_compaction",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "chronicle",
    "code_mode",
    "code_mode_only",
    "computer_use",
    "current_time_reminder",
    "default_mode_request_user_input",
    "deferred_executor",
    "enable_fanout",
    "enable_mcp_apps",
    "enable_request_compression",
    "exec_permission_approvals",
    "fast_mode",
    "goals",
    "guardian_approval",
    "hooks",
    "image_generation",
    "imagegenext",
    "in_app_browser",
    "item_ids",
    "local_thread_store_compression",
    "memories",
    "mentions_v2",
    "multi_agent",
    "multi_agent_v2",
    "network_proxy",
    "non_prefixed_mcp_tool_names",
    "personality",
    "plugin_sharing",
    "plugins",
    "prevent_idle_sleep",
    "realtime_conversation",
    "remote_compaction_v2",
    "remote_plugin",
    "request_permissions_tool",
    "respect_system_proxy",
    "rollout_budget",
    "runtime_metrics",
    "shell_snapshot",
    "shell_tool",
    "shell_zsh_fork",
    "skill_mcp_dependency_install",
    "sleep_tool",
    "standalone_web_search",
    "terminal_visualization_instructions",
    "token_budget",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unavailable_dummy_tools",
    "unified_exec",
    "unified_exec_zsh_fork",
    "use_agent_identity",
    "use_legacy_landlock",
    "web_search_cached",
    "web_search_request",
    "workspace_dependencies",
    "workspace_owner_usage_nudge",
)

CODEX_CONFIG_OVERRIDES = (
    'forced_login_method="chatgpt"',
    'history.persistence="none"',
    "hide_agent_reasoning=true",
    "show_raw_agent_reasoning=false",
    "check_for_update_on_startup=false",
    'web_search="disabled"',
    "mcp_servers={}",
    "apps._default.enabled=false",
    "analytics.enabled=false",
    "feedback.enabled=false",
    *(f"features.{feature}=false" for feature in DISABLED_CODEX_FEATURES),
)
