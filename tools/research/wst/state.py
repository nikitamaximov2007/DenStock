from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class WSTState:
    """Small local checkpoint database. It never stores Telegram access hashes or secrets."""

    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                message_id INTEGER PRIMARY KEY,
                content_hash TEXT NOT NULL,
                edit_date TEXT,
                discovery_source TEXT NOT NULL,
                processing_status TEXT NOT NULL DEFAULT 'discovered',
                error_message TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media (
                message_id INTEGER PRIMARY KEY,
                local_path TEXT,
                sha256 TEXT,
                download_status TEXT NOT NULL DEFAULT 'pending',
                extraction_status TEXT NOT NULL DEFAULT 'pending',
                transcription_status TEXT NOT NULL DEFAULT 'pending',
                ocr_status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS navigation_edges (
                parent_message_id INTEGER NOT NULL,
                child_message_id INTEGER,
                edge_type TEXT NOT NULL,
                link_label TEXT,
                link_order INTEGER NOT NULL,
                external_url TEXT,
                PRIMARY KEY (parent_message_id, link_order, edge_type)
            );
            """
        )
        self.connection.commit()

    def record_post(self, record: dict[str, Any]) -> bool:
        existing = self.connection.execute(
            "SELECT content_hash FROM posts WHERE message_id = ?", (record["message_id"],)
        ).fetchone()
        changed = existing is None or existing["content_hash"] != record["content_hash"]
        self.connection.execute(
            """
            INSERT INTO posts(
              message_id, content_hash, edit_date, discovery_source, processing_status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
              content_hash = excluded.content_hash,
              edit_date = excluded.edit_date,
              discovery_source = excluded.discovery_source,
              processing_status = CASE WHEN posts.content_hash != excluded.content_hash
                THEN 'discovered' ELSE posts.processing_status END,
              updated_at = excluded.updated_at
            """,
            (
                record["message_id"],
                record["content_hash"],
                record.get("edit_date"),
                record["discovery_source"],
                record.get("processing_status", "discovered"),
                _now(),
            ),
        )
        self.connection.commit()
        return changed

    def media_record(self, message_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM media WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_media(self, message_id: int, **values: Any) -> None:
        current = self.media_record(message_id) or {
            "message_id": message_id,
            "download_status": "pending",
            "extraction_status": "pending",
            "transcription_status": "pending",
            "ocr_status": "pending",
            "retry_count": 0,
        }
        current.update(values)
        current["updated_at"] = _now()
        columns = (
            "message_id",
            "local_path",
            "sha256",
            "download_status",
            "extraction_status",
            "transcription_status",
            "ocr_status",
            "retry_count",
            "error_message",
            "updated_at",
        )
        self.connection.execute(
            f"INSERT INTO media ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            + ", ".join(f"{column}=excluded.{column}" for column in columns[1:]),
            [current.get(column) for column in columns],
        )
        self.connection.commit()

    def replace_navigation_edges(self, parent_message_id: int, edges: list[dict[str, Any]]) -> None:
        self.connection.execute(
            "DELETE FROM navigation_edges WHERE parent_message_id = ?", (parent_message_id,)
        )
        self.connection.executemany(
            """
            INSERT INTO navigation_edges(
              parent_message_id, child_message_id, edge_type, link_label, link_order, external_url
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    parent_message_id,
                    edge.get("child_message_id"),
                    edge["edge_type"],
                    edge.get("link_label"),
                    edge["link_order"],
                    edge.get("external_url"),
                )
                for edge in edges
            ],
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> WSTState:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()
