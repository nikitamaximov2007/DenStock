from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tools.research.wst.navigation import (
    build_navigation_graph,
    extract_navigation_links,
    parse_internal_message_url,
)


def _message(message_id: int, text: str, entities=None, buttons=None):
    return SimpleNamespace(
        id=message_id, message=text, entities=entities or [], buttons=buttons or []
    )


def test_parse_private_channel_message_url_with_query() -> None:
    assert parse_internal_message_url("https://t.me/c/3278525266/123?single=1", 3278525266) == 123
    assert parse_internal_message_url("https://t.me/c/999/123", 3278525266) is None


def test_navigation_extracts_text_hidden_entity_and_button_links() -> None:
    hidden = SimpleNamespace(
        offset=len("Read https://t.me/c/3278525266/11 and "),
        length=6,
        url="https://t.me/c/3278525266/12",
    )
    button = SimpleNamespace(url="https://t.me/c/3278525266/13", text="Next")
    message = _message(3, "Read https://t.me/c/3278525266/11 and hidden", [hidden], [[button]])

    links = extract_navigation_links(message, 3278525266)

    assert [
        item["child_message_id"] for item in links if item["target_kind"] == "internal_message"
    ] == [11, 12, 13]
    assert [item["link_order"] for item in links] == [1, 2, 3]


def test_navigation_graph_is_cycle_safe_and_keeps_paths() -> None:
    messages = {
        3: _message(3, "https://t.me/c/3278525266/10"),
        10: _message(10, "https://t.me/c/3278525266/3 https://example.test/course"),
    }
    calls = []

    async def fetch(message_id: int):
        calls.append(message_id)
        return messages.get(message_id)

    graph = asyncio.run(build_navigation_graph(3, fetch, 3278525266))

    assert calls == [3, 10]
    assert graph["nodes"] == [
        {
            "message_id": 3,
            "navigation_path": [3],
            "parent_message_id": None,
            "discovery_source": "pinned_navigation",
            "status": "available",
        },
        {
            "message_id": 10,
            "navigation_path": [3, 10],
            "parent_message_id": 3,
            "discovery_source": "nested_navigation",
            "status": "available",
        },
    ]
    assert graph["external_links"][0]["edge_type"] == "external_link"
