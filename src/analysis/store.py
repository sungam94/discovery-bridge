"""sound.sqlite: one row per ISRC, written only by the analysis service."""
from __future__ import annotations

import json
import sqlite3
from array import array
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_sound (
  isrc TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('done', 'no_preview', 'error')),
  moods TEXT,          -- JSON {tag: score} for all 56 MTG-Jamendo mood/theme classes
  instrumental REAL,   -- 0..1
  embedding BLOB,      -- float32 mean Discogs-EffNet embedding, 1280 values
  analysed_at TEXT NOT NULL,
  error TEXT,
  deezer TEXT          -- JSON: Deezer's facts about the track (bpm, rank, release date, ...), without the preview URL
)"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def open_store(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(SCHEMA)
    if "deezer" not in [r[1] for r in conn.execute("PRAGMA table_info(track_sound)")]:
        conn.execute("ALTER TABLE track_sound ADD COLUMN deezer TEXT")   # files from before the column existed
    return conn


def save(conn: sqlite3.Connection, isrc: str, status: str, at: datetime, moods: dict | None = None,
         instrumental: float | None = None, embedding: list[float] | None = None, error: str | None = None,
         deezer: dict | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO track_sound(isrc, status, moods, instrumental, embedding, analysed_at, error, deezer) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (isrc, status, json.dumps(moods) if moods is not None else None, instrumental,
         array("f", embedding).tobytes() if embedding is not None else None, iso(at), error,
         json.dumps(deezer) if deezer is not None else None))


def save_deezer(conn: sqlite3.Connection, isrc: str, deezer: dict | None) -> None:
    """Facts for a track analysed before they were kept; {} marks 'Deezer knows nothing' so it is not asked again."""
    conn.execute("UPDATE track_sound SET deezer = ? WHERE isrc = ?", (json.dumps(deezer or {}), isrc))
