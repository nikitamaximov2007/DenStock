from __future__ import annotations

import json
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
            CREATE TABLE IF NOT EXISTS media_stages (
                message_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                backend TEXT,
                started_at TEXT,
                finished_at TEXT,
                error_class TEXT,
                error_message TEXT,
                diagnostic_details TEXT NOT NULL DEFAULT '{}',
                artifact_paths TEXT NOT NULL DEFAULT '[]',
                content_hash TEXT,
                next_action TEXT,
                PRIMARY KEY(message_id, stage)
            );
            CREATE TABLE IF NOT EXISTS media_downloads (
                message_id INTEGER PRIMARY KEY,
                expected_size INTEGER,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                verified_bytes INTEGER NOT NULL DEFAULT 0,
                chunk_size INTEGER NOT NULL,
                current_offset INTEGER NOT NULL DEFAULT 0,
                temporary_path TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_successful_offset INTEGER NOT NULL DEFAULT 0,
                last_progress_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                next_action TEXT
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

    def begin_stage(self, message_id: int, stage: str, *, backend: str = "") -> int:
        current = self.stage_record(message_id, stage) or {}
        attempts = int(current.get("attempts", 0)) + 1
        self.connection.execute(
            """
            INSERT INTO media_stages(message_id, stage, status, attempts, backend, started_at)
            VALUES (?, ?, 'running', ?, ?, ?)
            ON CONFLICT(message_id, stage) DO UPDATE SET
              status='running', attempts=excluded.attempts, backend=excluded.backend,
              started_at=excluded.started_at, finished_at=NULL,
              error_class=NULL, error_message=NULL, next_action=NULL
            """,
            (message_id, stage, attempts, backend, _now()),
        )
        self.connection.commit()
        return attempts

    def finish_stage(
        self,
        message_id: int,
        stage: str,
        *,
        status: str = "complete",
        backend: str = "",
        diagnostics: dict[str, Any] | None = None,
        artifacts: list[str] | None = None,
        content_hash: str | None = None,
        next_action: str = "",
    ) -> None:
        current = self.stage_record(message_id, stage) or {}
        self.connection.execute(
            """
            INSERT INTO media_stages(
              message_id, stage, status, attempts, backend, finished_at, diagnostic_details,
              artifact_paths, content_hash, next_action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, stage) DO UPDATE SET
              status=excluded.status, backend=excluded.backend, finished_at=excluded.finished_at,
              diagnostic_details=excluded.diagnostic_details,
              artifact_paths=excluded.artifact_paths,
              content_hash=excluded.content_hash, next_action=excluded.next_action
            """,
            (
                message_id,
                stage,
                status,
                int(current.get("attempts", 0)),
                backend,
                _now(),
                json.dumps(diagnostics or {}, ensure_ascii=False, sort_keys=True, default=str),
                json.dumps(artifacts or [], ensure_ascii=False),
                content_hash,
                next_action,
            ),
        )
        self.connection.commit()

    def fail_stage(
        self,
        message_id: int,
        stage: str,
        error: Exception | str,
        *,
        diagnostics: dict[str, Any] | None = None,
        artifacts: list[str] | None = None,
        retry: bool = True,
        next_action: str = "",
    ) -> None:
        message = str(error)
        error_class = (
            error.__class__.__name__ if isinstance(error, Exception) else "ProcessingError"
        )
        status = "retry_pending" if retry else "unrecoverable"
        self.finish_stage(
            message_id,
            stage,
            status=status,
            diagnostics={
                **(diagnostics or {}),
                "error_class": error_class,
                "error_message": message,
            },
            artifacts=artifacts,
            next_action=next_action or "Inspect diagnostics and rerun retry-media.",
        )
        self.connection.execute(
            "UPDATE media_stages SET error_class=?, error_message=? WHERE message_id=? AND stage=?",
            (error_class, message, message_id, stage),
        )
        self.connection.commit()

    def stage_record(self, message_id: int, stage: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM media_stages WHERE message_id=? AND stage=?", (message_id, stage)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["diagnostic_details"] = json.loads(result["diagnostic_details"] or "{}")
        result["artifact_paths"] = json.loads(result["artifact_paths"] or "[]")
        return result

    def retry_queue(
        self, *, stage: str | None = None, message_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["status='retry_pending'"]
        values: list[Any] = []
        if stage:
            clauses.append("stage=?")
            values.append(stage)
        if message_id:
            clauses.append("message_id=?")
            values.append(message_id)
        rows = self.connection.execute(
            "SELECT * FROM media_stages WHERE "
            + " AND ".join(clauses)
            + " ORDER BY message_id, stage",
            values,
        ).fetchall()
        return [self.stage_record(row["message_id"], row["stage"]) for row in rows]

    def progress(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT stage, status, COUNT(*) AS count FROM media_stages GROUP BY stage, status"
        ).fetchall()
        return {f"{row['stage']}:{row['status']}": int(row["count"]) for row in rows}

    def download_record(self, message_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM media_downloads WHERE message_id=?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def checkpoint_download(self, message_id: int, **values: Any) -> None:
        current = self.download_record(message_id) or {
            "message_id": message_id,
            "downloaded_bytes": 0,
            "verified_bytes": 0,
            "chunk_size": 0,
            "current_offset": 0,
            "attempt_count": 0,
            "last_successful_offset": 0,
            "status": "pending",
        }
        current.update(values)
        current["last_progress_at"] = _now()
        columns = (
            "message_id",
            "expected_size",
            "downloaded_bytes",
            "verified_bytes",
            "chunk_size",
            "current_offset",
            "temporary_path",
            "attempt_count",
            "last_successful_offset",
            "last_progress_at",
            "status",
            "last_error",
            "next_action",
        )
        self.connection.execute(
            f"INSERT INTO media_downloads ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            + ", ".join(f"{column}=excluded.{column}" for column in columns[1:]),
            [current.get(column) for column in columns],
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
