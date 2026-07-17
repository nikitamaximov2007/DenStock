import re
from urllib.parse import urlsplit

from django.conf import settings
from django.urls import Resolver404, resolve

ALLOWED_ROUTE_NAMES = frozenset(
    {
        "dashboard",
        "part_search",
        "part_list",
        "part_detail",
        "balance_list",
        "movement_list",
        "warehouse_index",
        "receipt_list",
        "receipt_detail",
        "scanner_receiving",
        "scanner_move",
        "counting_list",
        "counting_detail",
        "inventory_count_list",
        "inventory_count_detail",
        "actions_scan",
        "actions_report",
        "sale_list",
        "sale_detail",
        "reservation_list",
        "reservation_detail",
        "repair_order_list",
        "repair_order_detail",
        "return_list",
        "return_detail",
        "reports_dashboard",
        "statistics_dashboard",
        "ai_support:home",
        "ai_support:conversation",
    }
)

_VIEWPORT_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_BROWSERS = {"Chrome", "Edge", "Firefox", "Safari", "Other"}


def canonical_public_url() -> str:
    value = str(settings.DENSTOCK_PUBLIC_BASE_URL or "").strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return value.rstrip("/") + "/"


def safe_route_context(raw_path: str) -> dict[str, str]:
    path = (raw_path or "").strip()
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or "\\" in path
        or "://" in path
        or any(ord(char) < 32 for char in path)
    ):
        return {}
    try:
        match = resolve(path)
    except Resolver404:
        return {}
    route_name = match.view_name
    if route_name not in ALLOWED_ROUTE_NAMES:
        return {}
    return {"path": path[:500], "route_name": route_name}


def safe_diagnostic_snapshot(
    *, user, route_context: dict[str, str], browser_family: str = "", viewport: str = ""
) -> dict:
    browser = browser_family if browser_family in _BROWSERS else ""
    match = _VIEWPORT_RE.fullmatch(viewport or "")
    safe_viewport = ""
    if match and int(match.group(1)) <= 10000 and int(match.group(2)) <= 10000:
        safe_viewport = viewport
    roles = sorted(user.role_names) if not user.is_superuser else ["Администратор"]
    return {
        "path": route_context.get("path", ""),
        "route_name": route_context.get("route_name", ""),
        "roles": roles,
        "browser_family": browser,
        "viewport": safe_viewport,
        "app_commit": str(settings.DENSTOCK_APP_COMMIT or "")[:64],
        "public_base_url": canonical_public_url(),
    }
