"""Small SQL helpers shared by several modules. Feature-specific queries live with their feature."""
from __future__ import annotations

import sqlite3

_PAUSED_IF_MISSING = {"feedback_paused": True, "ingestion_paused": False}


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO setting(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def is_paused(conn: sqlite3.Connection, key: str) -> bool:
    value = get_setting(conn, key)
    if value is None:
        return _PAUSED_IF_MISSING.get(key, True)
    return value == "true"
