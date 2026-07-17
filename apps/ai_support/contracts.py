AUDITED_CODEX_CLI_VERSION = "0.142.5"


def normalize_provider_name(value: object) -> str:
    return str(value or "").strip().lower()
