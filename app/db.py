"""SQLite storage with one connection per operation for thread-safe worker usage."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_SETTINGS = {"concurrency": 3, "connections_per_download": 4, "speed_limit_kbps": 0, "auto_retries": 3}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.write_lock = threading.RLock()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.file_path), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.write_lock, self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT 'Preparing download',
                    kind TEXT NOT NULL DEFAULT 'auto',
                    status TEXT NOT NULL DEFAULT 'queued',
                    quality TEXT NOT NULL DEFAULT 'best',
                    quality_label TEXT NOT NULL DEFAULT 'Best available',
                    priority INTEGER NOT NULL DEFAULT 0,
                    filename TEXT,
                    final_path TEXT,
                    save_dir TEXT,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    speed REAL NOT NULL DEFAULT 0,
                    eta INTEGER,
                    duration REAL,
                    thumbnail TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    segments INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_status_priority
                    ON jobs (status, priority DESC, created_at ASC);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            # Idempotent schema migration for databases created before acceleration.
            column_names = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "segments" not in column_names:
                db.execute("ALTER TABLE jobs ADD COLUMN segments INTEGER NOT NULL DEFAULT 0")
            if "save_dir" not in column_names:
                db.execute("ALTER TABLE jobs ADD COLUMN save_dir TEXT")
            for key, value in DEFAULT_SETTINGS.items():
                db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            # Any transfer interrupted by an application restart can be resumed.
            db.execute("""UPDATE jobs SET status='paused', speed=0, eta=NULL,
                        error='Interrupted by application restart' WHERE status IN ('downloading', 'processing', 'pausing')""")

    def insert_job(self, values: dict[str, Any]) -> None:
        keys = list(values)
        with self.write_lock, self.connect() as db:
            db.execute(
                f"INSERT INTO jobs ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                tuple(values.values()),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if status:
                rows = db.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC", (status,))
            else:
                rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC")
            return [dict(row) for row in rows.fetchall()]

    def queued(self, limit: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY priority DESC, created_at ASC LIMIT ?", (limit,)
            ).fetchall()]

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        keys = list(fields)
        with self.write_lock, self.connect() as db:
            db.execute(f"UPDATE jobs SET {', '.join(k+'=?' for k in keys)} WHERE id=?", (*fields.values(), job_id))

    def set_status_if(self, job_id: str, expected: tuple[str, ...], status: str, **fields: Any) -> bool:
        fields.update({"status": status, "updated_at": now()})
        with self.write_lock, self.connect() as db:
            placeholders = ','.join('?' for _ in expected)
            keys = list(fields)
            cursor = db.execute(
                f"UPDATE jobs SET {', '.join(k+'=?' for k in keys)} WHERE id=? AND status IN ({placeholders})",
                (*fields.values(), job_id, *expected),
            )
            return cursor.rowcount > 0

    def delete_job(self, job_id: str) -> None:
        with self.write_lock, self.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def get_settings(self) -> dict[str, Any]:
        with self.connect() as db:
            return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key, value FROM settings")}

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.write_lock, self.connect() as db:
            for key, value in values.items():
                db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        return self.get_settings()
