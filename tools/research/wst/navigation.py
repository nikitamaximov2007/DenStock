from __future__ import annotations

import re
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

INTERNAL_LINK_RE = re.compile(
    r"https?://t\.me/c/(?P<channel>\d+)/(?P<message>\d+)(?:[/?#][^\s]*)?", re.IGNORECASE
)
URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)


def parse_internal_message_url(url: str, channel_id: int) -> int | None:
    match = INTERNAL_LINK_RE.fullmatch(url.strip().rstrip(".,;:!"))
    if not match or int(match.group("channel")) != int(channel_id):
        return None
    return int(match.group("message"))


def extract_navigation_links(message: Any, channel_id: int) -> list[dict[str, Any]]:
    """Extract visible, hidden and button URLs while preserving their source order."""
    text = str(getattr(message, "message", None) or getattr(message, "text", "") or "")
    candidates: list[tuple[int, str, str, str]] = []
    for match in URL_RE.finditer(text):
        candidates.append((match.start(), "text_url", match.group(0), match.group(0)))
    for entity in getattr(message, "entities", None) or []:
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        label = text[offset : offset + length]
        url = getattr(entity, "url", None)
        if url:
            candidates.append((offset, "entity_text_url", str(url), label))
        elif entity.__class__.__name__.endswith("MessageEntityUrl") and label:
            candidates.append((offset, "entity_url", label, label))
    button_order = len(candidates) + 10_000
    for row in getattr(message, "buttons", None) or []:
        for button in row if isinstance(row, (list, tuple)) else [row]:
            url = getattr(button, "url", None)
            if url:
                candidates.append(
                    (button_order, "button_url", str(url), str(getattr(button, "text", "")))
                )
                button_order += 1

    links: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for order, (_position, edge_type, url, label) in enumerate(sorted(candidates), start=1):
        key = (edge_type, url, label)
        if key in seen:
            continue
        seen.add(key)
        child_id = parse_internal_message_url(url, channel_id)
        parsed = urlparse(url)
        is_telegram = parsed.netloc.lower() in {"t.me", "www.t.me", "telegram.me"}
        if child_id is not None:
            links.append(
                {
                    "edge_type": edge_type,
                    "link_order": order,
                    "link_label": label,
                    "target_kind": "internal_message",
                    "child_message_id": child_id,
                }
            )
        elif is_telegram:
            links.append(
                {
                    "edge_type": "external_telegram_link",
                    "link_order": order,
                    "link_label": label,
                    "target_kind": "external_telegram",
                    "external_url": url,
                }
            )
        else:
            links.append(
                {
                    "edge_type": "external_link",
                    "link_order": order,
                    "link_label": label,
                    "target_kind": "external",
                    "external_url": url,
                }
            )
    return links


async def build_navigation_graph(
    root_message_id: int,
    fetch_message: Callable[[int], Awaitable[Any | None]],
    channel_id: int,
) -> dict[str, Any]:
    """Recursively walk only explicit in-channel links, with deterministic paths."""
    queue: deque[tuple[int, list[int], str, int | None]] = deque(
        [(root_message_id, [root_message_id], "pinned_navigation", None)]
    )
    visited: set[int] = set()
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    external_links: list[dict[str, Any]] = []
    while queue:
        message_id, path, discovery_source, parent_id = queue.popleft()
        if message_id in visited:
            continue
        visited.add(message_id)
        message = await fetch_message(message_id)
        if message is None:
            nodes.append(
                {
                    "message_id": message_id,
                    "navigation_path": path,
                    "parent_message_id": parent_id,
                    "discovery_source": discovery_source,
                    "status": "unavailable",
                }
            )
            continue
        nodes.append(
            {
                "message_id": message_id,
                "navigation_path": path,
                "parent_message_id": parent_id,
                "discovery_source": discovery_source,
                "status": "available",
            }
        )
        for link in extract_navigation_links(message, channel_id):
            edge = {"parent_message_id": message_id, **link}
            edges.append(edge)
            if link["target_kind"] == "internal_message":
                child_id = link["child_message_id"]
                if child_id not in visited:
                    queue.append((child_id, [*path, child_id], "nested_navigation", message_id))
            else:
                external_links.append(edge)
    return {
        "root_message_id": root_message_id,
        "channel_id": channel_id,
        "nodes": nodes,
        "edges": edges,
        "external_links": external_links,
    }
